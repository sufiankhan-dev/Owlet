"""Phase 10 end-to-end test against a mock Moodle site.

Run with:  $env:RUN_BROWSER_TESTS="1"; python -m pytest tests/test_end_to_end.py -q

A visible Chrome window opens, logs into a local mock Moodle, and the real tool
handlers run the complete study workflow: courses -> assignment -> PDF download
-> PDF reading -> Word/PDF/code generation -> workspace persistence. No model
call is made here, so the result is deterministic.
"""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import fitz
import pytest

from app.config import load_settings
from app.security import PathJail
from browser.moodle import parse_assignment_detail, take_snapshot
from browser.session import BrowserSession, BrowserState
from database.db import Database
from tools import build_registry
from tools.base import ToolContext, ToolError

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_BROWSER_TESTS") != "1",
    reason="Set RUN_BROWSER_TESTS=1 to launch a visible browser",
)

COURSES = """<!doctype html><html><head><title>My courses</title></head>
<body class="path-my"><div id="region-main">
<h1>My courses</h1>
<a href="/course/view.php?id=2">Database Systems</a>
<a href="/course/view.php?id=3">Software Quality Engineering</a>
<a href="/login/logout.php">Log out</a>
</div></body></html>"""

COURSE = """<!doctype html><html><head><title>Database Systems</title></head>
<body class="path-course-view"><div id="region-main">
<h1>Database Systems</h1>
<ul>
<li class="activity modtype_assign">
  <div class="activityname"><a href="/mod/assign/view.php?id=10">Normalization Exercise</a></div>
  <div data-region="activity-dates">Due: 12 November 2026, 11:59 PM</div>
  <div class="submissionstatus">Not submitted</div>
</li>
<li class="activity modtype_assign">
  <div class="activityname"><a href="/mod/assign/view.php?id=20">LAB TASK 1</a></div>
  <div data-region="activity-dates">Due: 19 November 2026, 11:59 PM</div>
  <div class="submissionstatus">Not submitted</div>
</li>
<li class="activity modtype_resource">
  <div class="activityname"><a href="/mod/resource/view.php?id=11">LAB 1</a></div>
</li>
</ul><a href="/login/logout.php">Log out</a>
</div></body></html>"""

ASSIGNMENT = """<!doctype html><html><head><title>Normalization Exercise</title></head>
<body class="path-mod-assign"><div id="region-main">
<nav aria-label="Breadcrumb"><a href="/my/">Dashboard</a>
<a href="/course/view.php?id=2">Database Systems</a></nav>
<h1>Normalization Exercise</h1>
<div id="intro"><p>Normalize the relation StudentCourse(student_id, course_id, student_name,
course_name, instructor) to 3NF. Explain each normal form and submit a PDF.</p></div>
<div data-region="activity-dates">Due: 12 November 2026, 11:59 PM</div>
<div class="submissionstatustable">Submission status: No attempt</div>
<a href="/pluginfile.php/99/mod_assign/intro/instructions.pdf">instructions.pdf</a>
<a href="/login/logout.php">Log out</a>
</div></body></html>"""

LAB_ASSIGNMENT = """<!doctype html><html><head><title>LAB TASK 1</title></head>
<body class="path-mod-assign"><div id="region-main">
<nav aria-label="Breadcrumb"><a href="/my/">Dashboard</a>
<a href="/course/view.php?id=2">Database Systems</a></nav>
<h1>LAB TASK 1</h1>
<div id="intro"><p>Complete LAB 1 using the lab sheet. Normalize the relation and
submit a short report.</p></div>
<div data-region="activity-dates">Due: 19 November 2026, 11:59 PM</div>
<div class="submissionstatustable">Submission status: No attempt</div>
<a href="/login/logout.php">Log out</a>
</div></body></html>"""

RESOURCE_LAB1 = """<!doctype html><html><head><title>LAB 1</title></head>
<body class="path-mod-resource"><div id="region-main">
<h1>LAB 1</h1>
<div id="intro"><p>LAB 1 tasks: normalize the StudentCourse relation to 3NF.</p></div>
<a href="/pluginfile.php/11/mod_resource/content/0/lab1.pdf">lab1.pdf</a>
<a href="/login/logout.php">Log out</a>
</div></body></html>"""


def _make_pdf(text: str) -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((60, 80), text, fontsize=11)
    data = document.tobytes()
    document.close()
    return data


PDF_BYTES = _make_pdf(
    "Normalization Exercise\n\nRelation: StudentCourse(student_id, course_id,\n"
    "student_name, course_name, instructor)\n\nTask: normalize to 3NF."
)
LAB_PDF_BYTES = _make_pdf(
    "LAB 1\n\nTask: normalize the StudentCourse relation to 3NF.\n"
    "Explain each normal form."
)


class _Handler(BaseHTTPRequestHandler):
    def _send_pdf(self, body: bytes, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/pluginfile.php/99/mod_assign/intro/instructions.pdf":
            self._send_pdf(PDF_BYTES, "instructions.pdf")
            return
        if path == "/pluginfile.php/11/mod_resource/content/0/lab1.pdf":
            self._send_pdf(LAB_PDF_BYTES, "lab1.pdf")
            return

        if path == "/course/view.php":
            body = COURSE
        elif path == "/mod/assign/view.php":
            body = LAB_ASSIGNMENT if query.get("id", [""])[0] == "20" else ASSIGNMENT
        elif path == "/mod/resource/view.php":
            body = RESOURCE_LAB1
        else:
            body = COURSES
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args) -> None:
        return


@pytest.fixture()
def mock_moodle() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_full_assignment_workflow(tmp_path: Path, mock_moodle: str) -> None:
    settings = replace(
        load_settings(),
        project_root=tmp_path,
        browser_profile_dir=tmp_path / "browser" / "profile",
        moodle_url=None,
    )
    settings.ensure_directories()

    async def scenario() -> None:
        events: list[tuple[str, str]] = []
        session = BrowserSession(
            settings, lambda kind, message, _data: events.append((kind, message))
        )
        db = Database(tmp_path / "database" / "e2e.db")
        approvals: list[tuple[str, dict]] = []

        async def approve(action: str, details: dict) -> bool:
            approvals.append((action, details))
            return False  # exercise the denial path for overwrites

        ctx = ToolContext(
            settings=settings,
            db=db,
            browser=session,
            jail=PathJail(
                {"workspace": settings.workspace_dir, "generated": settings.generated_dir}
            ),
            emit=lambda kind, message, data=None: events.append((kind, message)),
            approve=approve,
        )
        registry = build_registry()

        async def call(name: str, **kwargs):
            spec = registry.get(name)
            assert spec is not None, name
            return await spec.handler(ctx, spec.args_model(**kwargs))

        try:
            await session.start(f"{mock_moodle}/my/courses.php")
            assert session.status.state == BrowserState.READY
            assert session.status.logged_in is True

            courses = await call("list_courses")
            assert courses["count"] == 2
            assert {c["name"] for c in courses["courses"]} == {
                "Database Systems",
                "Software Quality Engineering",
            }

            opened = await call("open_course", course_name="Database Systems")
            assert opened["pending_count"] == 2
            names = [item["name"] for item in opened["assignments"]]
            assert set(names) == {"Normalization Exercise", "LAB TASK 1"}
            # The course-wide scan downloaded the separate LAB 1 resource.
            resources = settings.workspace_dir / "Database Systems" / "course_resources"
            assert (resources / "manifest.json").is_file()
            assert (resources / "section_1" / "lab1.pdf").read_bytes().startswith(b"%PDF")

            assignment = await call(
                "open_assignment", assignment_name="Normalization Exercise"
            )
            assert "3NF" in assignment["instructions"]
            assert assignment["assignment"]["course_name"] == "Database Systems"
            workspace = assignment["workspace"]["workspace"]
            assert workspace == "Database Systems/Normalization Exercise"
            instructions_file = (
                settings.workspace_dir / workspace / "instructions" / "assignment.md"
            )
            assert instructions_file.exists()
            assert "Normalization Exercise" in instructions_file.read_text(encoding="utf-8")

            attachment = assignment["assignment"]["attachments"][0]
            assert attachment["href"].endswith("instructions.pdf")

            downloaded = await call("download_attachment", url=attachment["href"])
            assert downloaded["filename"] == "instructions.pdf"
            saved_pdf = settings.workspace_dir / downloaded["path"]
            assert saved_pdf.read_bytes().startswith(b"%PDF")

            parsed = await call("read_pdf", path=downloaded["path"])
            assert "StudentCourse" in parsed["text"]
            assert "3NF" in parsed["text"]

            # A second download must ask for approval and must not overwrite on denial.
            before = saved_pdf.read_bytes()
            with pytest.raises(ToolError) as denied:
                await call("download_attachment", url=attachment["href"])
            assert "declined" in str(denied.value).lower()
            assert saved_pdf.read_bytes() == before
            assert approvals and approvals[0][0] == "overwrite_file"

            word = await call(
                "create_word_document",
                title="Normalization to 3NF",
                filename="solution.docx",
                content=(
                    "# Solution\n\n## Steps\n"
                    "1. Identify the functional dependencies.\n"
                    "2. Remove partial dependencies for 2NF.\n"
                    "3. Remove transitive dependencies for 3NF.\n\n"
                    "The relation becomes Student, Course, and Enrollment.\n"
                ),
            )
            assert word["path"].endswith("documents/solution.docx")
            assert (settings.workspace_dir / word["path"]).exists()

            pdf = await call(
                "create_pdf",
                title="Normalization summary",
                filename="summary.pdf",
                content="# Summary\n\n- Student(student_id, student_name)\n"
                "- Course(course_id, course_name, instructor)\n"
                "- Enrollment(student_id, course_id)\n",
            )
            assert pdf["pages"] >= 1
            assert (settings.workspace_dir / pdf["path"]).exists()

            code = await call(
                "create_code_file",
                filename="normalize.py",
                language="python",
                code=(
                    "def normalize(rows):\n"
                    "    students = {(r['student_id'], r['student_name']) for r in rows}\n"
                    "    courses = {(r['course_id'], r['course_name'], r['instructor']) for r in rows}\n"
                    "    enrolments = {(r['student_id'], r['course_id']) for r in rows}\n"
                    "    return students, courses, enrolments\n"
                ),
            )
            assert code["path"].endswith("code/normalize.py")
            code_file = settings.workspace_dir / code["path"]
            assert "def normalize" in code_file.read_text(encoding="utf-8")
            assert code["message"].endswith("It was not executed.")

            refreshed = await call(
                "save_assignment_workspace",
                notes="Saved during the end-to-end test.",
                overwrite=False,
            )
            assert refreshed["workspace"]["existing_not_overwritten"]
            assert (settings.workspace_dir / refreshed["workspace"]["workspace"]).exists()

            detail_snapshot = await take_snapshot(session.page)
            detail = parse_assignment_detail(detail_snapshot)
            assert detail["name"] == "Normalization Exercise"

            assert db.list_courses()
            assert db.list_assignments()[0]["name"] == "Normalization Exercise"
            assert any(kind == "file" for kind, _message in events)
            assert any(kind == "download" for kind, _message in events)

            # A separate LAB 1 resource in the same course is matched to LAB TASK 1
            # and copied with its original filename.
            lab = await call(
                "open_assignment",
                assignment_name="LAB TASK 1",
                course_name="Database Systems",
            )
            assert lab["assignment"]["name"] == "LAB TASK 1"
            assert "lab1.pdf" in {item["filename"] for item in lab["reference_materials"]}
            lab_base = settings.workspace_dir / "Database Systems" / "LAB TASK 1"
            assert (lab_base / "reference_materials" / "lab1.pdf").read_bytes().startswith(
                b"%PDF"
            )
            assert (lab_base / "extracted" / "references" / "lab1.txt").is_file()
        finally:
            await session.close()
            db.close()

    asyncio.run(scenario())
