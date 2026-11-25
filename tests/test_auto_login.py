"""Opt-in automatic Moodle login against a local mock Moodle.

Run with:  $env:RUN_BROWSER_TESTS="1"; python -m pytest tests/test_auto_login.py -q
A real Chrome window will open and close during the test.
"""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from app.config import load_settings
from browser.session import BrowserSession, BrowserState

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_BROWSER_TESTS") != "1",
    reason="Set RUN_BROWSER_TESTS=1 to launch a visible browser",
)

LOGIN_HTML = b"""<!doctype html><html><head><title>Log in to Moodle</title></head>
<body><h1>Log in</h1><form method="post" action="/login/index.php">
<input name="logintoken" type="hidden" value="abc123">
<input name="username" type="text"><input name="password" type="password">
<button type="submit">Log in</button></form></body></html>"""

SSO_HTML = b"""<!doctype html><html><head><title>Sign in</title></head>
<body><h1>Sign in</h1><form method="post" action="https://sso.example.edu/auth">
<input name="username" type="text"><input name="password" type="password">
<button type="submit">Sign in</button></form></body></html>"""

PREFILLED_HTML = b"""<!doctype html><html><head><title>Log in to Moodle</title></head>
<body><h1>Log in</h1><form method="post" action="/login/index.php">
<input name="logintoken" type="hidden" value="abc123">
<input name="username" type="text" value="someone.else">
<input name="password" type="password">
<button type="submit">Log in</button></form></body></html>"""

DASHBOARD_HTML = b"""<!doctype html><html><head><title>Dashboard</title></head>
<body><nav><button id="user-menu-toggle">Student Name</button>
<a href="/login/logout.php">Log out</a></nav>
<h1>My courses</h1></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    posts: list[dict[str, str]] = []
    login_html = LOGIN_HTML
    expected_username = "student"
    expected_password = "secret"

    def _send(self, body: bytes, status: int = 200, location: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/my/"):
            self._send(DASHBOARD_HTML)
        else:
            self._send(self.login_html)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        values = {key: items[0] for key, items in form.items() if items}
        type(self).posts.append(values)
        if (
            values.get("username") == self.expected_username
            and values.get("password") == self.expected_password
        ):
            self._send(b"", status=303, location="/my/")
            return
        self._send(self.login_html)

    def log_message(self, *_args) -> None:
        return


@pytest.fixture()
def mock_moodle() -> str:
    _Handler.posts = []
    _Handler.login_html = LOGIN_HTML
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _settings(
    tmp_path: Path,
    mock_moodle: str,
    *,
    auto_login: bool,
    password: str = "secret",
):
    return replace(
        load_settings(),
        browser_profile_dir=tmp_path / "profile",
        moodle_url=f"{mock_moodle}/",
        lms_auto_login=auto_login,
        lms_username="student" if auto_login else "",
        lms_password=password if auto_login else "",
    )


def test_auto_login_fills_the_plain_moodle_form(tmp_path: Path, mock_moodle: str) -> None:
    settings = _settings(tmp_path, mock_moodle, auto_login=True)

    async def scenario() -> None:
        session = BrowserSession(settings)
        try:
            status = await session.start(f"{mock_moodle}/login/index.php")
            assert status.logged_in is True
            assert status.state == BrowserState.READY
            assert len(_Handler.posts) == 1
            assert _Handler.posts[0]["logintoken"] == "abc123"
        finally:
            await session.close()

    asyncio.run(scenario())


def test_a_wrong_password_is_tried_once_only(tmp_path: Path, mock_moodle: str) -> None:
    settings = _settings(tmp_path, mock_moodle, auto_login=True, password="wrong")

    async def scenario() -> None:
        session = BrowserSession(settings)
        try:
            status = await session.start(f"{mock_moodle}/login/index.php")
            assert status.logged_in is False
            assert status.state == BrowserState.LOGIN_REQUIRED
            assert len(_Handler.posts) == 1

            # Status polling must never become a retry loop.
            for _ in range(3):
                await session.refresh_status()
            assert len(_Handler.posts) == 1
        finally:
            await session.close()

    asyncio.run(scenario())


def test_login_stays_manual_when_the_flag_is_off(tmp_path: Path, mock_moodle: str) -> None:
    settings = _settings(tmp_path, mock_moodle, auto_login=False)

    async def scenario() -> None:
        session = BrowserSession(settings)
        try:
            status = await session.start(f"{mock_moodle}/login/index.php")
            assert status.logged_in is False
            assert status.state == BrowserState.LOGIN_REQUIRED
            assert _Handler.posts == []
        finally:
            await session.close()

    asyncio.run(scenario())


def test_sso_like_pages_are_never_filled(tmp_path: Path, mock_moodle: str) -> None:
    _Handler.login_html = SSO_HTML
    settings = _settings(tmp_path, mock_moodle, auto_login=True)

    async def scenario() -> None:
        session = BrowserSession(settings)
        try:
            status = await session.start(f"{mock_moodle}/login/index.php")
            assert status.logged_in is False
            assert status.state == BrowserState.LOGIN_REQUIRED
            assert _Handler.posts == []
        finally:
            await session.close()

    asyncio.run(scenario())


def test_a_form_a_human_started_is_left_alone(tmp_path: Path, mock_moodle: str) -> None:
    _Handler.login_html = PREFILLED_HTML
    settings = _settings(tmp_path, mock_moodle, auto_login=True)

    async def scenario() -> None:
        session = BrowserSession(settings)
        try:
            status = await session.start(f"{mock_moodle}/login/index.php")
            assert status.logged_in is False
            assert _Handler.posts == []
        finally:
            await session.close()

    asyncio.run(scenario())

