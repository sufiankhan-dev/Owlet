"""Download Moodle attachments into the assignment workspace.

Read-only with respect to Moodle: we only fetch files the logged-in user can
already see. Local writes never overwrite an existing file without approval.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pydantic import Field

from app.security import sanitize_filename, validate_http_url, UrlSecurityError
from browser.files import MoodleDownloadError, fetch_moodle_file
from browser.moodle import take_snapshot
from browser.session import BrowserError
from tools.base import StrictArgs, ToolContext, ToolError, ToolRegistry, ToolSpec
from tools.extract import index_attachments
from tools.file_utils import write_bytes_atomic
from tools.workspace import assignment_workspace

logger = logging.getLogger(__name__)

_CONTENT_DISPOSITION_STAR = re.compile(r"filename\*\s*=\s*UTF-8''([^;]+)", re.IGNORECASE)
_CONTENT_DISPOSITION_PLAIN = re.compile(r'filename\s*=\s*"?([^";]+)"?', re.IGNORECASE)


class DownloadArgs(StrictArgs):
    url: str = Field(
        description=(
            "Absolute Moodle attachment URL (pluginfile.php, mod/resource/view.php, or "
            "mod/folder/view.php) taken from the assignment page."
        )
    )
    filename: str = Field(
        default="",
        description="Optional output filename. Leave empty to use the server's filename.",
    )
    course_name: str = Field(default="", description="Course name; defaults to the open course.")
    assignment_name: str = Field(
        default="", description="Assignment name; defaults to the open assignment."
    )


def filename_from_disposition(header: str | None) -> str | None:
    if not header:
        return None
    star = _CONTENT_DISPOSITION_STAR.search(header)
    if star:
        return unquote(star.group(1)).strip()
    plain = _CONTENT_DISPOSITION_PLAIN.search(header)
    if plain:
        return plain.group(1).strip()
    return None


def _filename_from_url(url: str) -> str:
    name = unquote(Path(urlparse(url).path).name)
    if name and name not in {"pluginfile.php", "view.php", "download.php"}:
        return name
    return "attachment"


async def _resolve_attachment_url(
    ctx: ToolContext, url: str
) -> tuple[str, str, str | None]:
    """Return (direct_file_url, suggested_name, return_url_or_none).

    A ``mod/resource/view.php`` link is left alone: Moodle redirects it straight
    to the protected ``pluginfile.php`` URL, and the download helper follows that
    redirect with the authenticated session. Only a folder page needs parsing,
    because it lists several files.
    """

    lowered = url.lower()
    if "/mod/folder/view.php" not in lowered:
        return url, "", None

    try:
        await ctx.browser.navigate(url)
    except BrowserError as exc:
        raise ToolError(str(exc)) from exc

    snapshot = await take_snapshot(ctx.browser.page, max_chars=ctx.settings.max_page_chars)
    if snapshot.get("page_type") == "login":
        raise ToolError("Moodle asked you to log in again. Finish logging in manually in Chrome.")

    candidates = [
        link
        for link in snapshot.get("links", [])
        if link.get("kind") == "attachment" or "pluginfile.php" in link.get("href", "")
    ]
    if len(candidates) == 1:
        link = candidates[0]
        return link["href"], link.get("text", ""), None
    if len(candidates) > 1:
        raise ToolError(
            "That folder contains several files. Choose one and call download_attachment "
            "for each file.",
            data={
                "candidates": [
                    {"text": c.get("text", ""), "url": c.get("href", "")}
                    for c in candidates[:20]
                ]
            },
        )
    raise ToolError(
        "No downloadable file was found on that folder page. It may be empty or use an "
        "external link."
    )


async def download_attachment(ctx: ToolContext, args: DownloadArgs) -> dict[str, Any]:
    if not ctx.browser.is_running:
        raise ToolError("The browser is not connected. Connect it and log in first.")

    try:
        url = validate_http_url(args.url, allow_loopback=True)
    except UrlSecurityError as exc:
        raise ToolError(f"Download blocked: {exc}") from exc

    if not ctx.browser.policy.permits(url):
        raise ToolError("Download blocked: the file is not hosted on your Moodle site.")

    assignment = ctx.current_assignment() or {}
    course = ctx.current_course() or {}
    course_name = args.course_name.strip() or assignment.get("course_name") or course.get("name", "")
    assignment_name = args.assignment_name.strip() or assignment.get("name", "")
    if not course_name or not assignment_name:
        raise ToolError(
            "Open the assignment first, or pass course_name and assignment_name, so the file "
            "is saved in the right workspace."
        )

    # A failed authentication or a removed file is not retried in a loop. The
    # model gets the reason and the next step instead of hammering Moodle.
    attempts: dict[str, dict[str, Any]] = ctx.state.setdefault("download_attempts", {})
    previous = attempts.get(url) or {}
    if previous.get("kind") in {"auth", "not_found"} or int(previous.get("count") or 0) >= 2:
        raise ToolError(
            f"{previous.get('error') or 'This download already failed.'} "
            "I will not retry the same URL again in this run. Continue with the other "
            "materials, or ask the user to fix the login/remove the dead link.",
            data={"already_attempted": True, "url": url, "kind": previous.get("kind", "")},
        )

    ctx.check_cancel()
    ctx.log("download", f"Fetching attachment: {urlparse(url).path.rsplit('/', 1)[-1]}")
    direct_url, link_text, _return_url = await _resolve_attachment_url(ctx, url)

    try:
        downloaded = await fetch_moodle_file(
            ctx.browser,
            direct_url,
            max_bytes=ctx.settings.max_download_bytes,
            timeout_ms=ctx.settings.browser_timeout_ms,
            referer=assignment.get("url") or course.get("url") or ctx.browser.page.url,
        )
    except MoodleDownloadError as exc:
        attempts[url] = {
            "kind": exc.kind,
            "error": exc.message,
            "count": int(previous.get("count") or 0) + 1,
        }
        raise ToolError(
            exc.message,
            data={
                "kind": exc.kind,
                "status": exc.status or 0,
                "already_attempted": exc.kind in {"auth", "not_found"},
                "next_step": (
                    "Ask the user to log in again in Chrome, then start a new request."
                    if exc.kind == "auth"
                    else "Continue with the other course resources and report this file."
                ),
            },
        ) from exc

    headers = {
        "content-type": downloaded.content_type,
        "content-disposition": downloaded.content_disposition,
    }
    content_type = downloaded.content_type
    body = downloaded.body

    suggested = (
        args.filename.strip()
        or filename_from_disposition(headers.get("content-disposition"))
        or _filename_from_url(downloaded.final_url)
        or link_text
        or "attachment"
    )
    filename = sanitize_filename(suggested, fallback="attachment")
    if "." not in filename:
        guessed = mimetypes.guess_extension(content_type or "")
        if guessed:
            filename += guessed

    base, relative = assignment_workspace(
        ctx.jail, course_name, assignment_name, ctx.known_courses()
    )
    target = base / "attachments" / filename

    if target.exists():
        approved = await ctx.require_approval(
            "overwrite_file",
            {
                "path": f"{relative}/attachments/{filename}",
                "size_bytes": target.stat().st_size,
                "reason": "An attachment with this name already exists.",
            },
        )
        if not approved:
            raise ToolError(
                "The user declined overwriting the existing attachment. "
                "Pass a different filename if you need a second copy."
            )

    write_bytes_atomic(target, body)
    relative_path = f"{relative}/attachments/{filename}"
    db_id = assignment.get("db_id")
    ctx.db.record_file(relative_path, len(body), "attachment", db_id)
    ctx.log("file", f"Saved attachment: {relative_path}", {"bytes": len(body)})

    # Make the file readable as text right away (extracted/<name>.txt, plus a
    # section in instructions/assignment.md), so the model can read it next.
    text_path = None
    try:
        index_attachments(base)
        expected = f"extracted/{filename}.txt"
        if (base / expected).is_file():
            text_path = f"{relative}/{expected}"
    except Exception:
        logger.debug("Attachment text extraction failed", exc_info=True)

    message = f"Downloaded {filename} ({len(body)} bytes) to the assignment workspace."
    if text_path:
        message += f" Its text is in {text_path}; read it with read_document."
    return {
        "ok": True,
        "path": relative_path,
        "filename": filename,
        "bytes": len(body),
        "content_type": content_type or "unknown",
        "text_path": text_path,
        "message": message,
    }


def register_attachment_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="download_attachment",
            description=(
                "Download a file linked on the assignment page into workspace/<course>/<assignment>/"
                "attachments/. Use the URL from the assignment details. Read-only on Moodle; "
                "asks before overwriting a local file."
            ),
            args_model=DownloadArgs,
            handler=download_attachment,
            read_only=True,
        )
    )
