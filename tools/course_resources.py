"""Course-wide resource discovery, download, and assignment matching.

Moodle does not keep a lab sheet next to the assignment that uses it: the sheet
is usually a separate ``mod/resource`` activity in another section. This module
scans every section of a course, downloads the teacher's files into a stable
``<course>/course_resources/section_N/`` tree, records a manifest, and ranks
those resources against an assignment before copying the relevant ones into
``<assignment>/reference_materials/``.

Nothing here submits, uploads, deletes, or changes Moodle data. Local writes are
new files only; an existing file is reused instead of overwritten.
"""

from __future__ import annotations

import asyncio
import filecmp
import hashlib
import json
import logging
import mimetypes
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from app.security import PathJail, sanitize_filename
from browser.files import MoodleDownloadError, fetch_moodle_file
from browser.moodle import (
    MoodleLoginLost,
    MoodleParseError,
    fetch_course_outline,
)
from browser.session import BrowserError
from tools.attachment_tools import _filename_from_url, filename_from_disposition
from tools.base import ToolContext, ToolError
from tools.extract import ExtractionError, extract_text
from tools.file_utils import write_bytes_atomic, write_text_atomic
from tools.workspace import canonical_course_name, course_folder

logger = logging.getLogger(__name__)

COURSE_RESOURCES_DIR = "course_resources"
MANIFEST_NAME = "manifest.json"
MATCHES_NAME = "matches.json"
#: Reuse a manifest this long before scanning the course again.
DISCOVERY_TTL_SECONDS = 3600
#: A scan downloads at most this many new files; the rest stay pending.
MAX_FILES_PER_SCAN = 120
_DOWNLOAD_CONCURRENCY = 3

_STATUS_DONE = {"downloaded", "existing"}
# ``auth`` is retried once in a *new* run (the user may have logged in again);
# within one run ``_download_one`` still refuses to repeat it.
_STATUS_RETRYABLE = {"auth", "network", "server"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(timezone.utc)


def course_resource_paths(
    jail: PathJail,
    course_name: str,
    known_courses: Iterable[str] = (),
) -> tuple[Path, str]:
    """Return ``(absolute course_resources dir, workspace-relative path)``."""

    name = canonical_course_name(course_name, known_courses)
    course_base, course_relative = course_folder(jail, name)
    base = course_base / COURSE_RESOURCES_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base, f"{course_relative}/{COURSE_RESOURCES_DIR}"


def manifest_path(base: Path) -> Path:
    return base / MANIFEST_NAME


def load_manifest(base: Path) -> dict[str, Any]:
    path = manifest_path(base)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.debug("Unreadable course resource manifest", exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def save_manifest(base: Path, manifest: dict[str, Any]) -> None:
    write_text_atomic(
        manifest_path(base), json.dumps(manifest, ensure_ascii=False, indent=2)
    )


def _cache_key(course: dict[str, Any]) -> str:
    return f"{str(course.get('url') or '').strip()}|{str(course.get('course_id') or '').strip()}"


def _manifest_from_outline(
    outline: dict[str, Any], *, course: dict[str, Any], relative: str
) -> dict[str, Any]:
    sections = list(outline.get("sections", []))
    resources = list(outline.get("resources", []))
    files: list[dict[str, Any]] = []
    for resource in resources:
        for item in resource.get("files", []):
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            files.append(
                {
                    "filename": str(item.get("name") or "").strip()[:250],
                    "activity_id": resource.get("activity_id", ""),
                    "activity_type": resource.get("activity_type", ""),
                    "activity_name": resource.get("name", ""),
                    "section": resource.get("section"),
                    "section_name": resource.get("section_name", ""),
                    "section_folder": resource.get("section_folder", ""),
                    "url": url,
                    "activity_url": resource.get("url", ""),
                    "content_type": item.get("content_type", ""),
                    "disposition": item.get("disposition", ""),
                    "local_path": "",
                    "relative": "",
                    "status": "pending",
                    "error": "",
                    "kind": "",
                    "bytes": 0,
                    "downloaded_at": "",
                }
            )
    return {
        "course": course.get("name", ""),
        "course_url": course.get("url", ""),
        "course_id": course.get("course_id", ""),
        "workspace": relative,
        "scanned_at": _utc_now(),
        "sections": sections,
        "resources": resources,
        "files": files,
    }


def _summary(
    manifest: dict[str, Any], *, relative: str, cached: bool = False
) -> dict[str, Any]:
    files = manifest.get("files", [])
    counts: dict[str, int] = {}
    for item in files:
        status = str(item.get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
    errors = [
        f"{item.get('filename') or item.get('url')}: {item.get('error')}"
        for item in files
        if item.get("status") == "failed" and item.get("error")
    ]
    for resource in manifest.get("resources", []):
        if resource.get("error") and not resource.get("files"):
            errors.append(
                f"{resource.get('name') or resource.get('url')}: {resource['error']}"
            )
    return {
        "ok": True,
        "cached": cached,
        "course": manifest.get("course", ""),
        "course_url": manifest.get("course_url", ""),
        "manifest_path": f"{relative}/{MANIFEST_NAME}",
        "scanned_at": manifest.get("scanned_at", ""),
        "sections": len(manifest.get("sections", [])),
        "resources": len(manifest.get("resources", [])),
        "files": len(files),
        "downloaded": counts.get("downloaded", 0),
        "reused": counts.get("existing", 0),
        "failed": counts.get("failed", 0),
        "pending": counts.get("pending", 0),
        "errors": errors[:20],
        "auth_error": manifest.get("auth_error", ""),
    }


def _manifest_is_fresh(manifest: dict[str, Any], *, course_url: str) -> bool:
    if not manifest or manifest.get("course_url") != course_url:
        return False
    scanned = _parse_iso(str(manifest.get("scanned_at") or ""))
    if scanned is None:
        return False
    return datetime.now(timezone.utc) - scanned <= timedelta(seconds=DISCOVERY_TTL_SECONDS)


def _existing_target(base: Path, entry: dict[str, Any]) -> Path | None:
    expected = int(entry.get("bytes") or 0)

    def usable(candidate: Path) -> bool:
        if not candidate.is_file() or candidate.stat().st_size <= 0:
            return False
        # A recorded size that no longer matches means the file was replaced or
        # truncated; download it again instead of trusting the stale copy.
        return not expected or candidate.stat().st_size == expected

    relative = str(entry.get("relative") or "").strip()
    if relative:
        candidate = base / relative
        if usable(candidate):
            return candidate
    folder = str(entry.get("section_folder") or "").strip()
    filename = str(entry.get("filename") or "").strip()
    if folder and filename:
        candidate = base / folder / sanitize_filename(filename, fallback="attachment")
        if usable(candidate):
            return candidate
    return None


def _unique_target(folder: Path, filename: str, *, activity_id: str = "") -> Path:
    """Pick a new filename when a different file already owns this name."""

    target = folder / filename
    if not target.exists():
        return target
    suffix = target.suffix
    stem = target.stem
    for label in (activity_id, "2", "3", "4", "5"):
        if not label:
            continue
        candidate = folder / f"{stem} ({label}){suffix}"
        if not candidate.exists():
            return candidate
    index = 6
    while True:
        candidate = folder / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
        index += 1


async def _download_one(
    ctx: ToolContext,
    *,
    base: Path,
    relative: str,
    entry: dict[str, Any],
    course_url: str,
) -> str:
    """Download one manifest entry. Returns ``downloaded``, ``existing``, ``failed`` or ``skipped``."""

    url = str(entry.get("url") or "")
    if not url:
        entry.update(status="failed", kind="blocked", error="The resource has no file URL.")
        return "failed"
    policy = getattr(ctx.browser, "policy", None)
    if policy is not None and not policy.permits(url):
        entry.update(
            status="failed",
            kind="blocked",
            error="That file is not hosted on your Moodle site, so it was not downloaded.",
        )
        return "failed"

    existing = _existing_target(base, entry)
    if existing is not None:
        entry["status"] = "existing"
        entry["relative"] = existing.relative_to(base).as_posix()
        entry["local_path"] = f"{relative}/{entry['relative']}"
        entry["bytes"] = existing.stat().st_size
        entry.pop("error", None)
        entry.pop("kind", None)
        return "existing"

    attempts: dict[str, dict[str, Any]] = ctx.state.setdefault("download_attempts", {})
    previous = attempts.get(url) or {}
    if previous.get("kind") in {"auth", "not_found"}:
        entry.update(
            status="failed",
            kind=previous.get("kind", ""),
            error=str(previous.get("error") or "This download already failed."),
        )
        return "skipped"
    if int(previous.get("count") or 0) >= 2:
        entry.update(
            status="failed",
            kind=str(previous.get("kind") or "network"),
            error=(
                f"{previous.get('error') or 'This download failed twice'}. "
                "I will not retry it again in this run."
            ),
        )
        return "skipped"

    try:
        downloaded = await fetch_moodle_file(
            ctx.browser,
            url,
            max_bytes=ctx.settings.max_download_bytes,
            timeout_ms=ctx.settings.browser_timeout_ms,
            referer=str(entry.get("activity_url") or course_url),
        )
    except MoodleDownloadError as exc:
        attempts[url] = {
            "kind": exc.kind,
            "error": exc.message,
            "count": int(previous.get("count") or 0) + 1,
        }
        entry.update(
            status="failed",
            kind=exc.kind,
            error=exc.message,
            status_code=exc.status or 0,
        )
        return "failed"
    except Exception as exc:  # a browser failure must not abort the whole scan
        logger.debug("Unexpected download failure for %s", url, exc_info=True)
        attempts[url] = {
            "kind": "network",
            "error": str(exc),
            "count": int(previous.get("count") or 0) + 1,
        }
        entry.update(status="failed", kind="network", error=str(exc))
        return "failed"

    suggested = (
        filename_from_disposition(downloaded.content_disposition)
        or _filename_from_url(downloaded.final_url)
        or str(entry.get("filename") or "")
        or "attachment"
    )
    filename = sanitize_filename(suggested, fallback="attachment")
    if "." not in filename:
        extension = mimetypes.guess_extension(downloaded.content_type or "")
        if extension:
            filename += extension

    folder_name = str(entry.get("section_folder") or "section_1")
    folder = base / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    target = _unique_target(folder, filename, activity_id=str(entry.get("activity_id") or ""))
    write_bytes_atomic(target, downloaded.body)

    entry.update(
        filename=target.name,
        relative=target.relative_to(base).as_posix(),
        local_path=f"{relative}/{target.relative_to(base).as_posix()}",
        status="downloaded",
        error="",
        kind="",
        bytes=len(downloaded.body),
        content_type=downloaded.content_type,
        sha256=hashlib.sha256(downloaded.body).hexdigest(),
        downloaded_at=_utc_now(),
    )
    try:
        ctx.db.record_file(entry["local_path"], len(downloaded.body), "resource", None)
    except Exception:
        logger.debug("Could not record course resource in SQLite", exc_info=True)
    ctx.log(
        "download",
        f"Saved course resource: {entry['local_path']}",
        {"bytes": len(downloaded.body), "activity_id": entry.get("activity_id")},
    )
    return "downloaded"


async def _download_files(
    ctx: ToolContext,
    *,
    base: Path,
    relative: str,
    manifest: dict[str, Any],
    course_url: str,
    max_files: int,
) -> dict[str, int]:
    """Download pending/failed resources with a small concurrency limit."""

    files = list(manifest.get("files", []))
    # A previous run may have failed on an expired login. That error is stale
    # once the user logs in again, so only this pass may report one.
    manifest.pop("auth_error", None)
    counts = {"downloaded": 0, "existing": 0, "failed": 0, "skipped": 0}
    budget = max(0, max_files)
    auth_error = ""
    semaphore = asyncio.Semaphore(_DOWNLOAD_CONCURRENCY)

    async def run(entry: dict[str, Any]) -> None:
        nonlocal budget, auth_error
        if auth_error:
            return
        ctx.check_cancel()
        if entry.get("status") in _STATUS_DONE:
            return
        if entry.get("status") == "failed" and entry.get("kind") not in _STATUS_RETRYABLE:
            counts["skipped"] += 1
            return
        if budget <= 0:
            entry["status"] = "pending"
            entry["error"] = (
                "Not downloaded yet: this scan reached its file limit. "
                "Open the course again with refresh_resources=true to continue."
            )
            counts["skipped"] += 1
            return
        budget -= 1
        async with semaphore:
            result = await _download_one(
                ctx, base=base, relative=relative, entry=entry, course_url=course_url
            )
        counts[result] = counts.get(result, 0) + 1
        if result == "failed" and entry.get("kind") == "auth":
            auth_error = str(entry.get("error") or "")

    await asyncio.gather(*(run(entry) for entry in files))
    if auth_error:
        manifest["auth_error"] = auth_error
    return counts


async def ensure_course_resources(
    ctx: ToolContext,
    course: dict[str, Any],
    *,
    refresh: bool = False,
    download: bool = True,
    max_files: int = MAX_FILES_PER_SCAN,
) -> dict[str, Any]:
    """Discover and download every resource in ``course``, reusing a fresh manifest.

    Raises ``ToolError`` only when the course itself cannot be read. Individual
    file failures are recorded in the manifest and returned in ``errors`` so one
    inaccessible attachment never aborts an assignment workflow.
    """

    if not ctx.browser.is_running:
        raise ToolError(
            "The browser is not connected. Connect it and log in before scanning a course."
        )
    course_url = str(course.get("url") or "").strip()
    if not course_url:
        raise ToolError("That course has no Moodle URL, so its resources cannot be scanned.")

    cache: dict[str, dict[str, Any]] = ctx.state.setdefault("course_resources", {})
    key = _cache_key(course)
    if not refresh and key in cache:
        return {**cache[key], "cached": True}

    base, relative = course_resource_paths(
        ctx.jail, str(course.get("name") or "Course"), ctx.known_courses()
    )
    manifest = {} if refresh else load_manifest(base)

    if _manifest_is_fresh(manifest, course_url=course_url):
        summary = _summary(manifest, relative=relative, cached=True)
    else:
        ctx.check_cancel()
        ctx.log(
            "reading",
            f"Scanning every section of {course.get('name') or 'the course'}",
            {"course_url": course_url},
        )
        try:
            outline = await fetch_course_outline(
                ctx.browser.page,
                course_url,
                max_sections=40,
                max_activities=120,
                timeout_ms=ctx.settings.browser_timeout_ms,
            )
        except MoodleLoginLost as exc:
            raise ToolError(str(exc)) from exc
        except (MoodleParseError, BrowserError) as exc:
            raise ToolError(
                f"Could not scan the course resources: {exc}. Open the course page in "
                "Chrome, make sure you are logged in, then retry."
            ) from exc
        manifest = _manifest_from_outline(outline, course=course, relative=relative)
        save_manifest(base, manifest)
        summary = _summary(manifest, relative=relative)

    if download:
        counts = await _download_files(
            ctx,
            base=base,
            relative=relative,
            manifest=manifest,
            course_url=course_url,
            max_files=max_files,
        )
        save_manifest(base, manifest)
        summary = _summary(manifest, relative=relative, cached=summary.get("cached", False))
        summary.update(
            downloaded_now=counts["downloaded"],
            reused_now=counts["existing"],
            failed_now=counts["failed"],
            skipped_now=counts["skipped"],
        )
        ctx.log(
            "reading",
            (
                f"Course resources ready: {summary['downloaded']} saved, "
                f"{summary['reused']} reused, {summary['failed']} failed"
            ),
            {"manifest": summary["manifest_path"]},
        )

    cache[key] = summary
    return summary


def find_assignment_section(
    manifest: dict[str, Any], *, activity_id: str = "", assignment_url: str = ""
) -> int | None:
    """Locate the section that contains the assignment activity, if visible."""

    activity_id = str(activity_id or "").strip()
    for resource in manifest.get("resources", []):
        if activity_id and str(resource.get("activity_id") or "") == activity_id:
            try:
                return int(resource.get("section"))
            except (TypeError, ValueError):
                return None
        if assignment_url and str(resource.get("url") or "") == assignment_url:
            try:
                return int(resource.get("section"))
            except (TypeError, ValueError):
                return None
    return None


_ID_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("lab", re.compile(r"\blab(?:oratory)?\s*(?:task)?\s*[-_ ]?0*(\d{1,2})\b", re.I)),
    ("task", re.compile(r"\btask\s*[-_ ]?0*(\d{1,2})\b", re.I)),
    ("assignment", re.compile(r"\bassign(?:ment)?\s*[-_ ]?0*(\d{1,2})\b", re.I)),
    ("week", re.compile(r"\bweek\s*[-_ ]?0*(\d{1,2})\b", re.I)),
    ("exercise", re.compile(r"\bex(?:ercise)?\s*[-_ ]?0*(\d{1,2})\b", re.I)),
    ("module", re.compile(r"\bmodule\s*[-_ ]?0*(\d{1,2})\b", re.I)),
    ("number", re.compile(r"\b(?:no|number|#)\s*[-_ ]?0*(\d{1,2})\b", re.I)),
)

_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "your", "you", "are",
    "task", "lab", "laboratory", "assignment", "file", "pdf", "docx", "pptx",
    "final", "week", "part", "submission", "instructions", "instruction",
    "reference", "material", "materials", "course", "student", "name", "date",
    "due", "solution", "solutions", "answer", "answers", "sheet", "sheets",
}


def resource_identifiers(text: str) -> set[tuple[str, int]]:
    """Stable identifiers such as ``("lab", 1)`` and ``("task", 1)`` in a label."""

    found: set[tuple[str, int]] = set()
    for kind, pattern in _ID_PATTERNS:
        for match in pattern.finditer(str(text or "")):
            try:
                found.add((kind, int(match.group(1))))
            except (TypeError, ValueError):
                continue
    return found


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]{3,}", str(text or "").lower())
    return {word for word in words if word not in _STOPWORDS and not word.isdigit()}


def _name_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def rank_resources(
    assignment_name: str,
    instructions: str,
    candidates: list[dict[str, Any]],
    *,
    assignment_section: int | None = None,
    max_matches: int = 6,
) -> list[dict[str, Any]]:
    """Score course resources against one assignment, verifying file contents.

    ``candidates`` need ``name`` and/or ``filename``, an optional ``text``, and
    any metadata the caller wants echoed back. A resource whose readable text
    contradicts the assignment is dropped; a resource with no readable text is
    kept only when its identifiers or name match strongly, and is marked
    ``verified=None`` so the caller can warn instead of pretending.
    """

    assignment_ids = resource_identifiers(assignment_name)
    instruction_ids = resource_identifiers(str(instructions or "")[:2000])
    # The assignment name is authoritative; the brief adds context but can
    # mention other labs, so its identifiers weigh less.
    primary_ids = assignment_ids or instruction_ids
    assignment_tokens = _tokens(assignment_name)
    assignment_key = _name_key(assignment_name)

    texts = [str(item.get("text") or "") for item in candidates]
    token_sets = [_tokens(text) for text in texts]
    document_frequency: dict[str, int] = {}
    for tokens in token_sets:
        for token in tokens:
            document_frequency[token] = document_frequency.get(token, 0) + 1
    common = {
        token
        for token, count in document_frequency.items()
        if len(token_sets) >= 3 and count >= max(3, len(token_sets) // 2)
    }

    ranked: list[dict[str, Any]] = []
    for candidate, text, text_tokens in zip(candidates, texts, token_sets):
        name_blob = f"{candidate.get('name', '')} {candidate.get('filename', '')}"
        candidate_ids = resource_identifiers(name_blob) | resource_identifiers(text[:4000])
        name_tokens = _tokens(name_blob)
        reasons: list[str] = []
        score = 0

        candidate_key = _name_key(str(candidate.get("name") or candidate.get("filename") or ""))
        exact_name = bool(candidate_key) and candidate_key == assignment_key
        if exact_name:
            score += 10
            reasons.append("same name")

        for kind, number in primary_ids:
            if (kind, number) in candidate_ids:
                weight = {"lab": 8, "task": 5, "assignment": 4}.get(kind, 3)
                score += weight
                reasons.append(f"{kind} {number}")

        name_overlap = assignment_tokens & name_tokens
        if name_overlap:
            score += min(6, 2 * len(name_overlap))
            reasons.append("name: " + ", ".join(sorted(name_overlap)[:4]))

        content_overlap = (assignment_tokens & (text_tokens - common))
        if content_overlap:
            score += min(8, len(content_overlap))
            reasons.append("content: " + ", ".join(sorted(content_overlap)[:4]))

        content_ids = resource_identifiers(text[:4000])
        content_id_hits = primary_ids & content_ids
        if content_id_hits:
            score += 4 * len(content_id_hits)
            reasons.append(
                "content mentions " + ", ".join(f"{kind} {number}" for kind, number in sorted(content_id_hits))
            )

        same_section = (
            assignment_section is not None
            and candidate.get("section") == assignment_section
        )
        if same_section:
            score += 2
            reasons.append("same section")

        has_text = bool(text.strip())
        # Same-section alone is never enough, but a same-section file whose text
        # actually mentions the assignment topic is useful reference material.
        verified = (
            bool(
                exact_name
                or content_id_hits
                or len(content_overlap) >= 3
                or (same_section and content_overlap)
            )
            if has_text
            else None
        )
        strong_identity = bool(
            exact_name
            or (primary_ids & candidate_ids)
            or name_overlap
            or (same_section and content_overlap)
            or len(content_overlap) >= 2
        )
        if score < 6 or not strong_identity:
            continue
        if has_text and not verified:
            # The file was readable and did not support the match; do not copy it.
            continue

        ranked.append(
            {
                **candidate,
                "score": score,
                "reasons": reasons,
                "verified": verified,
                "text": text,
            }
        )

    ranked.sort(
        key=lambda item: (
            item["verified"] is True,
            item["score"],
            len(str(item.get("filename") or "")),
        ),
        reverse=True,
    )
    return ranked[:max_matches]


def _read_resource_text(ctx: ToolContext, path: Path) -> str:
    cache: dict[str, str] = ctx.state.setdefault("resource_text", {})
    key = str(path)
    if key in cache:
        return cache[key]
    try:
        text = extract_text(path, max_chars=12000)
    except ExtractionError:
        text = ""
    except OSError:
        text = ""
    cache[key] = text
    return text


def _cheap_match_score(
    candidate: dict[str, Any],
    *,
    assignment_ids: set[tuple[str, int]],
    assignment_tokens: set[str],
    assignment_section: int | None,
) -> int:
    """Name/section-only score, used to avoid extracting every course file."""

    blob = f"{candidate.get('name', '')} {candidate.get('filename', '')}"
    score = 0
    if assignment_ids & resource_identifiers(blob):
        score += 10
    overlap = assignment_tokens & _tokens(blob)
    if overlap:
        score += min(6, 2 * len(overlap))
    if assignment_section is not None and candidate.get("section") == assignment_section:
        score += 3
    return score


def match_resources(
    ctx: ToolContext,
    manifest: dict[str, Any],
    *,
    assignment_name: str,
    instructions: str,
    assignment_section: int | None = None,
    exclude_activity_id: str = "",
    max_matches: int = 6,
    max_text_reads: int = 24,
) -> list[dict[str, Any]]:
    """Build candidates from a manifest and rank them for this assignment.

    Text extraction is expensive on a laptop, so only the most plausible
    candidates (matching identifiers, name words, or section) are read before
    the real content check. A course with 100 files therefore does not cost 100
    PDF reads every time an assignment is opened.
    """

    candidates: list[dict[str, Any]] = []
    for entry in manifest.get("files", []):
        if entry.get("status") not in _STATUS_DONE:
            continue
        local_path = str(entry.get("local_path") or "")
        if not local_path:
            continue
        try:
            path = ctx.jail.resolve("workspace", local_path, must_exist=True)
        except Exception:
            continue
        candidates.append(
            {
                "name": entry.get("activity_name") or entry.get("filename") or "",
                "filename": entry.get("filename") or path.name,
                "local_path": local_path,
                "source_url": entry.get("url", ""),
                "activity_url": entry.get("activity_url", ""),
                "activity_id": entry.get("activity_id", ""),
                "activity_type": entry.get("activity_type", ""),
                "section": entry.get("section"),
                "section_name": entry.get("section_name", ""),
                "_path": path,
            }
        )

    # Page/book resources have useful text but no downloadable file.
    for resource in manifest.get("resources", []):
        if resource.get("files") or not resource.get("text"):
            continue
        candidates.append(
            {
                "name": resource.get("name", ""),
                "filename": "",
                "local_path": "",
                "source_url": resource.get("url", ""),
                "activity_url": resource.get("url", ""),
                "activity_id": resource.get("activity_id", ""),
                "activity_type": resource.get("activity_type", ""),
                "section": resource.get("section"),
                "section_name": resource.get("section_name", ""),
                "text": resource.get("text", ""),
            }
        )

    if exclude_activity_id:
        candidates = [
            item
            for item in candidates
            if str(item.get("activity_id") or "") != str(exclude_activity_id)
        ]
    if not candidates:
        return []

    assignment_ids = resource_identifiers(assignment_name) or resource_identifiers(
        str(instructions or "")[:2000]
    )
    assignment_tokens = _tokens(assignment_name)

    for candidate in candidates:
        candidate["_cheap"] = _cheap_match_score(
            candidate,
            assignment_ids=assignment_ids,
            assignment_tokens=assignment_tokens,
            assignment_section=assignment_section,
        )

    # Always keep identifier/name/section hits; add the best remaining ones so
    # a differently named handout can still be found by its contents.
    plausible = sorted(
        (item for item in candidates if item["_cheap"] > 0),
        key=lambda item: item["_cheap"],
        reverse=True,
    )
    rest = sorted(candidates, key=lambda item: item["_cheap"], reverse=True)
    selected = plausible[:max_text_reads]
    if len(selected) < min(max_text_reads, len(rest)):
        chosen = {id(item) for item in selected}
        for item in rest:
            if len(selected) >= max_text_reads:
                break
            if id(item) not in chosen:
                selected.append(item)

    for candidate in selected:
        path = candidate.pop("_path", None)
        if "text" not in candidate:
            candidate["text"] = _read_resource_text(ctx, path) if path is not None else ""
        candidate.pop("_cheap", None)

    return rank_resources(
        assignment_name,
        instructions,
        selected,
        assignment_section=assignment_section,
        max_matches=max_matches,
    )


def _unique_copy_target(folder: Path, filename: str, *, section: Any = None) -> Path:
    target = folder / filename
    if not target.exists():
        return target
    suffix = target.suffix
    stem = target.stem
    labels = [f"section {section}" if section is not None else "", "2", "3", "4", "5"]
    for label in labels:
        if not label:
            continue
        candidate = folder / f"{stem} ({label}){suffix}"
        if not candidate.exists():
            return candidate
    index = 6
    while True:
        candidate = folder / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def copy_matches_to_assignment(
    ctx: ToolContext,
    *,
    assignment_base: Path,
    assignment_relative: str,
    assignment_name: str,
    matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Copy matched resources into ``reference_materials/`` and record why."""

    reference = assignment_base / "reference_materials"
    reference.mkdir(parents=True, exist_ok=True)
    copied: list[dict[str, Any]] = []

    for match in matches:
        local_path = str(match.get("local_path") or "")
        text = str(match.get("text") or "")
        filename = str(match.get("filename") or "").strip()
        if not filename:
            filename = sanitize_filename(str(match.get("name") or "reference"), fallback="reference")
            if "." not in filename:
                filename += ".md"
        filename = sanitize_filename(filename, fallback="reference")

        target = reference / filename
        reused = False
        try:
            if local_path:
                source = ctx.jail.resolve("workspace", local_path, must_exist=True)
                if target.exists() and filecmp.cmp(source, target, shallow=False):
                    reused = True
                else:
                    if target.exists():
                        target = _unique_copy_target(
                            reference, filename, section=match.get("section")
                        )
                    temp = target.with_name(target.name + ".part")
                    shutil.copyfile(source, temp)
                    temp.replace(target)
            elif text.strip():
                body = text.strip() + "\n"
                if target.exists():
                    try:
                        reused = target.read_text(encoding="utf-8") == body
                    except OSError:
                        reused = False
                    if not reused:
                        target = _unique_copy_target(
                            reference, filename, section=match.get("section")
                        )
                if not reused:
                    write_text_atomic(target, body)
            else:
                continue
        except Exception as exc:
            logger.warning("Could not copy reference material %s", filename, exc_info=True)
            copied.append(
                {
                    **{key: match.get(key) for key in ("name", "filename", "score", "verified")},
                    "error": str(exc),
                    "path": "",
                }
            )
            continue

        relative_path = f"{assignment_relative}/reference_materials/{target.name}"
        try:
            ctx.db.record_file(
                relative_path, target.stat().st_size, "reference_material", None
            )
        except Exception:
            logger.debug("Could not record reference material in SQLite", exc_info=True)

        copied.append(
            {
                "name": match.get("name", ""),
                "filename": target.name,
                "path": relative_path,
                "source_url": match.get("source_url", ""),
                "activity_id": match.get("activity_id", ""),
                "activity_type": match.get("activity_type", ""),
                "section": match.get("section"),
                "section_name": match.get("section_name", ""),
                "score": match.get("score", 0),
                "verified": match.get("verified"),
                "reasons": match.get("reasons", []),
                "reused": reused,
                "error": "",
            }
        )

    record = {
        "assignment": assignment_name,
        "matched_at": _utc_now(),
        "matches": copied,
    }
    try:
        write_text_atomic(
            reference / MATCHES_NAME,
            json.dumps(record, ensure_ascii=False, indent=2),
        )
    except OSError:
        logger.warning("Could not write reference matches manifest", exc_info=True)

    return copied


def write_reference_extracts(
    ctx: ToolContext,
    *,
    assignment_base: Path,
    assignment_relative: str,
    copied: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Save readable text of copied references under ``extracted/references/``.

    Draft prompts list ``extracted/`` files, so this is what makes a matched lab
    sheet visible to the model without it re-reading every PDF by hand.
    """

    folder = assignment_base / "extracted" / "references"
    written: list[dict[str, Any]] = []
    for item in copied:
        relative = str(item.get("path") or "")
        if not relative:
            continue
        try:
            source = ctx.jail.resolve("workspace", relative, must_exist=True)
            text = extract_text(source, max_chars=20000)
        except (ExtractionError, OSError):
            continue
        except Exception:
            logger.debug("Reference text extraction failed for %s", relative, exc_info=True)
            continue
        if not text.strip():
            continue

        stem = sanitize_filename(source.stem, fallback="reference")
        target = folder / f"{stem}.txt"
        index = 2
        while target.exists():
            try:
                existing = target.read_text(encoding="utf-8")
            except OSError:
                existing = ""
            if existing == text:
                break
            target = folder / f"{stem} ({index}).txt"
            index += 1
        if not target.exists():
            folder.mkdir(parents=True, exist_ok=True)
            write_text_atomic(target, text + "\n")

        text_relative = f"{assignment_relative}/extracted/references/{target.name}"
        try:
            ctx.db.record_file(
                text_relative, target.stat().st_size, "reference_text", None
            )
        except Exception:
            logger.debug("Could not record reference text in SQLite", exc_info=True)
        item["text_path"] = text_relative
        written.append({"source": relative, "text_path": text_relative})

    return written
