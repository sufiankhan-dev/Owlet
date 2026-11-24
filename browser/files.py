"""Authenticated Moodle file downloads through the visible Chrome session.

Moodle often redirects a ``mod/resource/view.php`` link to a protected
``pluginfile.php`` URL. ``Playwright``'s request context shares cookies with the
browser, but it does not use Chrome's proxy/HTTP-Basic-auth cache. Campuses that
front Moodle with such a proxy therefore answer ``401`` even though the visible
tab is logged in.

This module tries the cheap request-context path first and falls back to a
``fetch`` issued from inside a Chrome page, which uses the exact same network
stack (cookies, proxy credentials, redirects) as the user's own browsing. It
never bypasses authentication: when Moodle really wants a login, the error says
so and the caller stops instead of retrying in a loop.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from browser.session import BrowserSession

logger = logging.getLogger(__name__)

_HTML_TYPES = {"text/html", "application/xhtml+xml"}


class MoodleDownloadError(RuntimeError):
    """A download failure with a user-facing reason and a stable ``kind``.

    ``kind`` is one of ``auth``, ``not_found``, ``too_large``, ``html``,
    ``network``, ``server`` or ``blocked``. Callers use it to decide whether a
    retry could possibly help.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "network",
        status: int | None = None,
        url: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status = status
        self.url = url


@dataclass(frozen=True)
class DownloadedFile:
    """One successfully fetched Moodle file."""

    url: str
    final_url: str
    body: bytes
    content_type: str
    content_disposition: str
    status: int


_IN_PAGE_FETCH_JS = r"""
async ({url, maxBytes}) => {
  let response;
  try {
    response = await fetch(url, {credentials: 'same-origin', redirect: 'follow'});
  } catch (error) {
    return {error: 'request_failed', detail: String(error).slice(0, 200)};
  }
  const headers = {};
  response.headers.forEach((value, key) => { headers[key.toLowerCase()] = value; });
  const finalUrl = response.url || url;
  const contentType = (headers['content-type'] || '').toLowerCase();
  if (/\/login\//i.test(finalUrl)) {
    return {login: true, status: response.status, finalUrl, contentType};
  }
  if (!response.ok) {
    return {status: response.status, headers, finalUrl, contentType};
  }
  const length = Number(headers['content-length'] || 0);
  if (length && length > maxBytes) {
    return {
      tooLarge: true, status: response.status, headers, finalUrl, contentType, bytes: length
    };
  }
  let buffer;
  try {
    buffer = await response.arrayBuffer();
  } catch (error) {
    return {error: 'body_failed', status: response.status, finalUrl, contentType};
  }
  if (buffer.byteLength > maxBytes) {
    return {
      tooLarge: true, status: response.status, headers, finalUrl, contentType,
      bytes: buffer.byteLength
    };
  }
  const bytes = new Uint8Array(buffer);
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  let encoded = '';
  try {
    encoded = btoa(binary);
  } catch (error) {
    return {error: 'encode_failed', status: response.status, finalUrl, contentType};
  }
  return {
    status: response.status, headers, finalUrl, contentType, base64: encoded,
    bytes: bytes.length
  };
}
"""


def _same_origin(page_url: str, target_url: str) -> bool:
    page = urlparse(page_url or "")
    target = urlparse(target_url or "")
    return bool(page.scheme and page.netloc and page.scheme == target.scheme and page.netloc == target.netloc)


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _looks_like_login_html(content_type: str, body: bytes) -> bool:
    if not any(item in (content_type or "") for item in _HTML_TYPES):
        return False
    sample = body[:30000].lower()
    return (
        b'type="password"' in sample
        or b"login/index.php" in sample
        or b"log in to the site" in sample
        or b'name="logintoken"' in sample
    )


def _status_error(status: int, *, url: str) -> MoodleDownloadError:
    if status in {401, 403}:
        return MoodleDownloadError(
            "Moodle rejected the download "
            f"(HTTP {status}). Your Moodle session may have expired, or a campus "
            "proxy needs the network login. Log in again in the visible Chrome window "
            "(and reconnect with the network username/password if your campus asks "
            "for one), then retry once. Do not repeat this download in a loop.",
            kind="auth",
            status=status,
            url=url,
        )
    if status == 404:
        return MoodleDownloadError(
            "That file is no longer available (HTTP 404). The teacher may have removed "
            "it or the link expired. Continue with the other materials and tell the user.",
            kind="not_found",
            status=status,
            url=url,
        )
    if status >= 500:
        return MoodleDownloadError(
            f"Moodle's server returned HTTP {status} for that file. Try again later; "
            "this is a server-side problem, not a missing file.",
            kind="server",
            status=status,
            url=url,
        )
    return MoodleDownloadError(
        f"Moodle returned HTTP {status} for that file. The link may be stale or need "
        "a different login.",
        kind="network",
        status=status,
        url=url,
    )


def _from_raw(raw: dict, *, url: str, max_bytes: int) -> DownloadedFile:
    status = int(raw.get("status") or 0)
    final_url = str(raw.get("finalUrl") or url)
    headers = raw.get("headers") or {}
    content_type = str(raw.get("contentType") or headers.get("content-type") or "").split(";")[0].strip().lower()
    disposition = str(headers.get("content-disposition") or "")

    if raw.get("login"):
        raise MoodleDownloadError(
            "Moodle redirected the download to its login page. Your session has expired; "
            "log in again in the visible Chrome window, then retry once.",
            kind="auth",
            status=status or None,
            url=url,
        )
    if raw.get("tooLarge"):
        size = int(raw.get("bytes") or 0)
        raise MoodleDownloadError(
            f"That file is larger than the {max_bytes // (1024 * 1024)} MB download limit"
            + (f" ({size} bytes)." if size else "."),
            kind="too_large",
            status=status or None,
            url=url,
        )
    if raw.get("error"):
        raise MoodleDownloadError(
            f"The download could not be completed ({raw.get('error')}).",
            kind="network",
            status=status or None,
            url=url,
        )
    if status >= 400:
        raise _status_error(status, url=url)
    if any(item in content_type for item in _HTML_TYPES):
        raise MoodleDownloadError(
            "Moodle returned a web page instead of a file. This link is a view page, "
            "not a direct download. Open the course resource list and use the file link.",
            kind="html",
            status=status or None,
            url=url,
        )
    encoded = str(raw.get("base64") or "")
    if not encoded:
        raise MoodleDownloadError(
            "Moodle returned an empty response for that file.",
            kind="network",
            status=status or None,
            url=url,
        )
    try:
        body = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise MoodleDownloadError(
            "The downloaded file could not be decoded safely.",
            kind="network",
            status=status or None,
            url=url,
        ) from exc
    if len(body) > max_bytes:
        raise MoodleDownloadError(
            f"That file is larger than the {max_bytes // (1024 * 1024)} MB download limit.",
            kind="too_large",
            status=status or None,
            url=url,
        )
    return DownloadedFile(
        url=url,
        final_url=final_url,
        body=body,
        content_type=content_type,
        content_disposition=disposition,
        status=status or 200,
    )


async def _fetch_in_page(
    browser: BrowserSession,
    url: str,
    *,
    max_bytes: int,
    timeout_ms: int,
) -> DownloadedFile | None:
    """Fetch through Chrome itself; ``None`` when no usable page was available."""

    page = browser.page
    tab = None
    target = page
    try:
        if not _same_origin(page.url, url):
            # A cross-origin fetch is blocked by CORS. Open a short-lived tab on
            # the Moodle origin so Chrome's own session performs the request.
            tab = await page.context.new_page()
            await tab.goto(_origin(url) + "/my/", wait_until="domcontentloaded", timeout=timeout_ms)
            target = tab
        raw = await target.evaluate(
            _IN_PAGE_FETCH_JS, {"url": url, "maxBytes": max_bytes}
        )
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        logger.debug("In-page download failed for %s: %s", url, exc)
        return None
    finally:
        if tab is not None:
            try:
                await tab.close()
            except Exception:
                logger.debug("Could not close the download tab", exc_info=True)

    if not isinstance(raw, dict):
        return None
    return _from_raw(raw, url=url, max_bytes=max_bytes)


async def fetch_moodle_file(
    browser: BrowserSession,
    url: str,
    *,
    max_bytes: int,
    timeout_ms: int,
    referer: str = "",
) -> DownloadedFile:
    """Download ``url`` with the logged-in session, or raise ``MoodleDownloadError``.

    The request-context path is fast and is enough on ordinary Moodle sites. A
    401/403, a login redirect, an HTML body, or a network error triggers exactly
    one fallback through the visible Chrome page before the failure is reported.
    """

    headers = {"Referer": referer} if referer else {}
    first_error: MoodleDownloadError | None = None

    try:
        response = await browser.page.request.get(
            url,
            headers=headers,
            timeout=timeout_ms,
            max_redirects=10,
        )
        status = response.status
        content_type = (
            response.headers.get("content-type", "").split(";")[0].strip().lower()
        )
        body = await response.body()
        final_url = response.url or url
        if status < 400 and "/login/" not in final_url.lower():
            if any(item in content_type for item in _HTML_TYPES):
                if _looks_like_login_html(content_type, body):
                    first_error = MoodleDownloadError(
                        "Moodle redirected the download to its login page. Your session "
                        "has expired; log in again in Chrome, then retry once.",
                        kind="auth",
                        status=status,
                        url=url,
                    )
                else:
                    first_error = MoodleDownloadError(
                        "Moodle returned a web page instead of a file. This link is a "
                        "view page, not a direct download.",
                        kind="html",
                        status=status,
                        url=url,
                    )
            elif len(body) > max_bytes:
                first_error = MoodleDownloadError(
                    f"That file is larger than the {max_bytes // (1024 * 1024)} MB "
                    "download limit.",
                    kind="too_large",
                    status=status,
                    url=url,
                )
            elif not body:
                first_error = MoodleDownloadError(
                    "Moodle returned an empty response for that file. The link may be "
                    "stale; open the course resource and use its file link.",
                    kind="network",
                    status=status,
                    url=url,
                )
            else:
                return DownloadedFile(
                    url=url,
                    final_url=final_url,
                    body=body,
                    content_type=content_type,
                    content_disposition=response.headers.get("content-disposition", ""),
                    status=status,
                )
        elif "/login/" in final_url.lower():
            first_error = MoodleDownloadError(
                "Moodle redirected the download to its login page. Your session has "
                "expired; log in again in Chrome, then retry once.",
                kind="auth",
                status=status or None,
                url=url,
            )
        else:
            first_error = _status_error(status, url=url)
    except MoodleDownloadError as exc:
        first_error = exc
    except Exception as exc:  # Playwright's error hierarchy differs by version
        first_error = MoodleDownloadError(
            f"The download request failed ({exc}).",
            kind="network",
            url=url,
        )

    # One fallback only, and only for failures a browser session could fix. A
    # 404 or an oversized file is not retried through a second network path.
    assert first_error is not None
    if first_error.kind in {"auth", "network", "html"}:
        fallback = await _fetch_in_page(
            browser, url, max_bytes=max_bytes, timeout_ms=timeout_ms
        )
        if fallback is not None:
            return fallback

    raise first_error
