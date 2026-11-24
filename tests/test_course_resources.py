"""Fast tests for course-wide resource discovery and assignment matching.

No browser and no model: the Moodle outline is a plain dict, and the download
helper is exercised with a fake page so the 401 fallback path is deterministic.
"""

from __future__ import annotations

import asyncio
import base64
import json
import types
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import load_settings
from app.security import PathJail
from browser.files import MoodleDownloadError, fetch_moodle_file
from browser.moodle import map_course_outline
from database.db import Database
from tools.base import ToolContext, ToolError
from tools.browser_tools import _resolve_course
from tools.course_resources import (
    _download_one,
    copy_matches_to_assignment,
    course_resource_paths,
    rank_resources,
    resource_identifiers,
    write_reference_extracts,
)
from tools.workspace import assignment_workspace

COURSE_URL = "https://lms.example.edu/course/view.php?id=2"


def _context(tmp_path: Path) -> ToolContext:
    settings = replace(
        load_settings(),
        project_root=tmp_path,
        browser_profile_dir=tmp_path / "browser" / "profile",
        moodle_url=None,
    )
    settings.ensure_directories()
    return ToolContext(
        settings=settings,
        db=Database(tmp_path / "database" / "resources.db"),
        browser=types.SimpleNamespace(is_running=False),
        jail=PathJail(
            {"workspace": settings.workspace_dir, "generated": settings.generated_dir}
        ),
    )


def test_map_course_outline_keeps_sections_and_file_urls() -> None:
    raw = {
        "sections": [
            {
                "number": 0,
                "name": "General",
                "summary": "",
                "activities": [],
                "files": [],
            },
            {
                "number": 1,
                "name": "Week 1",
                "summary": "Start here",
                "activities": [
                    {
                        "name": "LAB 1",
                        "href": "/mod/resource/view.php?id=11",
                        "modtype": "resource",
                        "activityId": "11",
                        "files": [
                            {
                                "name": "lab1.pdf",
                                "href": "/pluginfile.php/11/mod_resource/content/0/lab1.pdf",
                            }
                        ],
                    },
                    {
                        "name": "LAB 1",
                        "href": "/mod/resource/view.php?id=11",
                        "modtype": "resource",
                        "activityId": "11",
                        "files": [],
                    },
                ],
                "files": [
                    {
                        "name": "handout.pdf",
                        "href": "/pluginfile.php/12/mod_resource/content/0/handout.pdf",
                    }
                ],
            },
        ]
    }

    outline = map_course_outline(raw, course_url=COURSE_URL)

    assert [section["folder"] for section in outline["sections"]] == [
        "section_1",
        "section_2",
    ]
    assert outline["sections"][1]["name"] == "Week 1"
    # The duplicate activity is ignored; the loose handout link becomes a resource.
    assert [item["name"] for item in outline["resources"]] == ["LAB 1", "handout.pdf"]
    lab = outline["resources"][0]
    assert lab["activity_id"] == "11"
    assert lab["section"] == 2
    assert lab["files"][0]["url"] == (
        "https://lms.example.edu/pluginfile.php/11/mod_resource/content/0/lab1.pdf"
    )
    assert outline["resources"][1]["files"][0]["url"].endswith("handout.pdf")


def test_identifiers_and_ranking_prefer_the_matching_lab() -> None:
    assert ("lab", 1) in resource_identifiers("LAB TASK 1")
    assert ("task", 1) in resource_identifiers("LAB TASK 1")

    candidates = [
        {
            "name": "LAB 1",
            "filename": "lab1.pdf",
            "section": 2,
            "text": "LAB 1 tasks: normalize the relation and explain 3NF.",
        },
        {
            "name": "LAB 2",
            "filename": "lab2.pdf",
            "section": 2,
            "text": "LAB 2 tasks: write SQL queries.",
        },
        {
            "name": "Lecture slides",
            "filename": "slides.pdf",
            "section": 1,
            "text": "Normalization lecture notes.",
        },
    ]

    matches = rank_resources(
        "LAB TASK 1",
        "Complete lab 1 and submit a PDF.",
        candidates,
        assignment_section=2,
    )

    assert matches
    assert matches[0]["name"] == "LAB 1"
    assert matches[0]["verified"] is True
    assert all(item["name"] != "LAB 2" for item in matches)


def test_a_same_name_file_with_unrelated_content_is_not_copied() -> None:
    matches = rank_resources(
        "LAB TASK 1",
        "Complete lab 1.",
        [
            {
                "name": "LAB 1",
                "filename": "cafeteria.pdf",
                "section": 1,
                "text": "This week's cafeteria menu and opening hours.",
            }
        ],
    )
    assert matches == []


def test_existing_download_is_reused_without_a_browser(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    base, relative = course_resource_paths(ctx.jail, "Database Systems")
    folder = base / "section_1"
    folder.mkdir(parents=True)
    (folder / "lab1.pdf").write_bytes(b"%PDF-1.4")

    entry = {
        "url": "https://lms.example.edu/pluginfile.php/11/lab1.pdf",
        "section_folder": "section_1",
        "filename": "lab1.pdf",
        "activity_id": "11",
    }
    result = asyncio.run(
        _download_one(
            ctx,
            base=base,
            relative=relative,
            entry=entry,
            course_url=COURSE_URL,
        )
    )

    assert result == "existing"
    assert entry["status"] == "existing"
    assert entry["local_path"] == "Database Systems/course_resources/section_1/lab1.pdf"


def test_matches_are_copied_with_original_names_and_extracted_text(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    resource_base, resource_relative = course_resource_paths(ctx.jail, "Database Systems")
    folder = resource_base / "section_1"
    folder.mkdir(parents=True)
    (folder / "lab1.pdf").write_bytes(b"%PDF-1.4")
    (folder / "lab1.txt").write_text("LAB 1: normalize the relation.", encoding="utf-8")

    assignment_base, assignment_relative = assignment_workspace(
        ctx.jail, "Database Systems", "LAB TASK 1"
    )
    copied = copy_matches_to_assignment(
        ctx,
        assignment_base=assignment_base,
        assignment_relative=assignment_relative,
        assignment_name="LAB TASK 1",
        matches=[
            {
                "name": "LAB 1",
                "filename": "lab1.pdf",
                "local_path": f"{resource_relative}/section_1/lab1.pdf",
                "section": 1,
                "score": 8,
                "verified": True,
                "reasons": ["lab 1"],
                "text": "",
            },
            {
                "name": "LAB 1 notes",
                "filename": "lab1.txt",
                "local_path": f"{resource_relative}/section_1/lab1.txt",
                "section": 1,
                "score": 6,
                "verified": True,
                "reasons": ["lab 1"],
                "text": "",
            },
        ],
    )

    reference = assignment_base / "reference_materials"
    assert (reference / "lab1.pdf").read_bytes() == b"%PDF-1.4"
    assert (reference / "lab1.txt").read_text(encoding="utf-8").startswith("LAB 1")
    record = json.loads((reference / "matches.json").read_text(encoding="utf-8"))
    assert [item["filename"] for item in record["matches"]] == ["lab1.pdf", "lab1.txt"]

    written = write_reference_extracts(
        ctx,
        assignment_base=assignment_base,
        assignment_relative=assignment_relative,
        copied=copied,
    )
    assert written
    assert (assignment_base / "extracted" / "references" / "lab1.txt").is_file()


class _FakeResponse:
    def __init__(self, *, status: int, body: bytes = b"", url: str) -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self.url = url
        self._body = body

    async def body(self) -> bytes:
        return self._body


class _FakeRequest:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def get(self, _url: str, **_kwargs) -> _FakeResponse:
        return self._response


class _FakePage:
    def __init__(self, response: _FakeResponse, evaluate_result) -> None:
        self.url = response.url
        self.request = _FakeRequest(response)
        self._evaluate_result = evaluate_result

    async def evaluate(self, _script: str, _args: dict):
        return self._evaluate_result


def _browser(page: _FakePage) -> types.SimpleNamespace:
    return types.SimpleNamespace(page=page)


def test_401_falls_back_to_the_authenticated_page_once() -> None:
    url = "https://lms.example.edu/pluginfile.php/11/lab1.pdf"
    payload = {
        "status": 200,
        "headers": {
            "content-type": "application/pdf",
            "content-disposition": 'attachment; filename="lab1.pdf"',
        },
        "finalUrl": url,
        "contentType": "application/pdf",
        "base64": base64.b64encode(b"%PDF-1.4").decode(),
        "bytes": 8,
    }
    page = _FakePage(_FakeResponse(status=401, url=url), payload)

    downloaded = asyncio.run(
        fetch_moodle_file(_browser(page), url, max_bytes=1024 * 1024, timeout_ms=1000)
    )

    assert downloaded.body == b"%PDF-1.4"
    assert downloaded.content_type == "application/pdf"


def test_401_with_login_redirect_reports_auth_without_retrying() -> None:
    url = "https://lms.example.edu/pluginfile.php/11/lab1.pdf"
    page = _FakePage(
        _FakeResponse(status=401, url=url),
        {"login": True, "status": 401, "finalUrl": url, "contentType": "text/html"},
    )

    with pytest.raises(MoodleDownloadError) as error:
        asyncio.run(
            fetch_moodle_file(_browser(page), url, max_bytes=1024 * 1024, timeout_ms=1000)
        )

    assert error.value.kind == "auth"
    assert "log in" in error.value.message.lower()


def test_401_without_a_usable_page_keeps_the_original_error() -> None:
    url = "https://lms.example.edu/pluginfile.php/11/lab1.pdf"
    page = _FakePage(_FakeResponse(status=401, url=url), None)

    with pytest.raises(MoodleDownloadError) as error:
        asyncio.run(
            fetch_moodle_file(_browser(page), url, max_bytes=1024 * 1024, timeout_ms=1000)
        )

    assert error.value.kind == "auth"
    assert error.value.status == 401


def test_login_redirect_is_reported_as_auth_even_without_a_page() -> None:
    url = "https://lms.example.edu/pluginfile.php/11/lab1.pdf"
    login_url = "https://lms.example.edu/login/index.php"
    page = _FakePage(_FakeResponse(status=200, url=login_url), None)

    with pytest.raises(MoodleDownloadError) as error:
        asyncio.run(
            fetch_moodle_file(_browser(page), url, max_bytes=1024 * 1024, timeout_ms=1000)
        )

    assert error.value.kind == "auth"
    assert "log in" in error.value.message.lower()


def test_empty_response_is_not_saved_as_a_file() -> None:
    url = "https://lms.example.edu/pluginfile.php/11/lab1.pdf"
    page = _FakePage(_FakeResponse(status=200, url=url), None)

    with pytest.raises(MoodleDownloadError) as error:
        asyncio.run(
            fetch_moodle_file(_browser(page), url, max_bytes=1024 * 1024, timeout_ms=1000)
        )

    assert error.value.kind == "network"
    assert "empty" in error.value.message.lower()


def test_course_id_wins_over_an_ambiguous_name() -> None:
    courses = [
        {"name": "CS-3301 A", "url": "https://lms.example.edu/course/view.php?id=10", "course_id": "10"},
        {"name": "CS-3301 B", "url": "https://lms.example.edu/course/view.php?id=11", "course_id": "11"},
    ]

    chosen = _resolve_course(courses, "CS-3301", "11")
    assert chosen["course_id"] == "11"

    with pytest.raises(ToolError):
        _resolve_course(courses, "CS-3301", "")

    with pytest.raises(ToolError):
        _resolve_course(courses, "", "999")
