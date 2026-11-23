"""Phase 9 tests: FastAPI surface, static dashboard, CSRF/host guards, downloads."""

from __future__ import annotations

import asyncio
import base64
import re
import shutil
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.llm import LLMResult
from app.api import create_app
from app.config import load_settings
from app.state import AppState
from tools.base import ToolContext, ToolError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UI_NAMES = ("index.html", "styles.css", "app.js")
EXPECTED_TOOLS = {
    "get_current_page",
    "get_page_content",
    "navigate_to_url",
    "list_courses",
    "open_course",
    "list_assignments",
    "open_assignment",
    "download_attachment",
    "read_pdf",
    "read_document",
    "create_word_document",
    "create_pdf",
    "create_code_file",
    "save_assignment_workspace",
}


class FakeLLM:
    """No network: health and a single deterministic answer."""

    def __init__(self, answer: str = "Hello from the fake model.") -> None:
        self.answer = answer
        self.calls = 0
        self.last_messages: list[dict] = []

    async def health(self) -> dict:
        return {
            "ok": True,
            "reachable": True,
            "host": "http://localhost:11434",
            "model": "qwen3.5:9b",
            "installed": True,
            "models": [
                {"name": "qwen3.5:9b", "size_bytes": 5_200_000_000, "modified_at": ""}
            ],
            "running": ["qwen3.5:9b"],
            "error": None,
            "latency_ms": 1.0,
        }

    async def stream_chat(self, messages, **kwargs):
        self.calls += 1
        self.last_messages = list(messages)
        on_delta = kwargs.get("on_delta")
        if on_delta:
            on_delta(self.answer[:10])
            on_delta(self.answer[10:])
        return LLMResult(content=self.answer)

    async def aclose(self) -> None:
        return None


@pytest.fixture()
def api(tmp_path: Path):
    settings = replace(
        load_settings(),
        project_root=tmp_path,
        browser_profile_dir=tmp_path / "browser" / "profile",
    )
    settings.ensure_directories()

    target_ui = tmp_path / "app" / "ui"
    target_ui.mkdir(parents=True, exist_ok=True)
    for name in UI_NAMES:
        shutil.copyfile(PROJECT_ROOT / "app" / "ui" / name, target_ui / name)

    state = AppState(settings, llm=FakeLLM())
    app = create_app(state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, state, settings


def test_ui_assets_are_served_with_security_headers(api) -> None:
    client, _state, _settings = api

    index = client.get("/")
    assert index.status_code == 200
    assert "text/html" in index.headers["content-type"]
    assert "Owlet" in index.text

    css = client.get("/styles.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert ":root" in css.text

    script = client.get("/app.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert "EventSource" in script.text

    for response in (index, css, script):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in response.headers["content-security-policy"]

    assert client.get("/favicon.ico").status_code == 204


def test_network_credentials_are_transient_only(api) -> None:
    client, _state, _settings = api

    index = client.get("/").text
    script = client.get("/app.js").text

    # The only password field in the UI is the optional HTTP Basic auth one.
    assert index.count('type="password"') == 1
    assert 'id="httpPass"' in index
    assert 'id="httpUser"' in index
    assert 'autocomplete="off"' in index

    # It travels to the connect route and is cleared as soon as the call returns.
    assert 'api("/api/browser/connect"' in script
    assert "http_username: httpUsername" in script
    assert "http_password: httpPassword" in script
    assert 'els.httpPass.value = ""' in script

    # Nothing sensitive is persisted: storage holds only the theme and which
    # cards are collapsed, each under its own fixed key.
    keys = re.findall(r"localStorage\.setItem\(\s*([A-Za-z_]+)", script)
    assert sorted(keys) == ["CARDS_KEY", "THEME_KEY"]
    assert 'const CARDS_KEY = "owlet-collapsed"' in script
    assert "sessionStorage" not in script


def test_state_and_tools_expose_only_implemented_capabilities(api) -> None:
    client, _state, _settings = api

    state = client.get("/api/state")
    assert state.status_code == 200
    payload = state.json()
    for key in (
        "ollama",
        "browser",
        "moodle_url",
        "agent",
        "courses",
        "assignments",
        "sync",
        "tools",
        "activity",
        "files",
        "approvals",
    ):
        assert key in payload
    assert payload["ollama"]["installed"] is True
    assert payload["browser"]["state"] == "disconnected"

    tools = client.get("/api/tools")
    assert tools.status_code == 200
    names = {tool["name"] for tool in tools.json()["tools"]}
    assert names == EXPECTED_TOOLS
    assert len(names) == 14


def test_mutating_requests_require_local_header(api) -> None:
    client, _state, _settings = api

    blocked = client.post("/api/agent/stop", json={})
    assert blocked.status_code == 403
    assert "header" in blocked.json()["detail"].lower()

    allowed = client.post("/api/agent/stop", json={}, headers={"X-Owlet": "1"})
    assert allowed.status_code == 200
    assert allowed.json()["ok"] is False


def test_sync_endpoint_asks_for_a_connection_instead_of_failing_silently(api) -> None:
    client, state, _settings = api

    blocked = client.post("/api/sync", json={})
    assert blocked.status_code == 403

    offline = client.post("/api/sync", json={}, headers={"X-Owlet": "1"})
    assert offline.status_code == 400
    assert "connect" in offline.json()["detail"].lower()
    assert state.sync_state()["running"] is False

    snapshot = client.get("/api/state").json()
    assert snapshot["sync"]["enabled"] is True
    assert snapshot["sync"]["last_synced_at"] is None


def test_non_local_host_and_origin_are_rejected(api) -> None:
    client, _state, _settings = api

    foreign_host = client.get("/api/version", headers={"host": "evil.example"})
    assert foreign_host.status_code == 403
    assert "local" in foreign_host.json()["detail"].lower()

    foreign_origin = client.get(
        "/api/version", headers={"Origin": "https://evil.example"}
    )
    assert foreign_origin.status_code == 403

    local = client.get("/api/version")
    assert local.status_code == 200
    assert local.json()["app"] == "Owlet"


def test_ipv6_loopback_host_is_accepted(api) -> None:
    client, _state, _settings = api

    # Browsers send bracket-and-port hosts; naive colon splitting broke IPv6.
    ipv6 = client.get("/api/version", headers={"host": "[::1]:8765"})
    assert ipv6.status_code == 200
    assert ipv6.json()["app"] == "Owlet"

    bare = client.get("/api/version", headers={"host": "::1"})
    assert bare.status_code == 200

    localhost_port = client.get("/api/version", headers={"host": "localhost:8765"})
    assert localhost_port.status_code == 200

    lookalike = client.get("/api/version", headers={"host": "127.0.0.1.evil.example"})
    assert lookalike.status_code == 403

    ipv6_origin = client.get(
        "/api/version", headers={"Origin": "http://[::1]:8765"}
    )
    assert ipv6_origin.status_code == 200


def test_workspace_tree_descends_without_repeating_the_root(api) -> None:
    client, _state, settings = api

    assignment = settings.workspace_dir / "Databases" / "Assignment 1"
    assignment.mkdir(parents=True, exist_ok=True)
    (assignment / "source.txt").write_text("material", encoding="utf-8")

    tree = client.get("/api/workspace").json()
    top_level = {node["name"]: node for node in tree["workspace"]}
    assert "Databases" in top_level

    databases = top_level["Databases"]
    assert [child["name"] for child in databases["children"]] == ["Assignment 1"]
    files = databases["children"][0]["children"]
    assert [node["name"] for node in files] == ["source.txt"]
    assert files[0]["path"] == "Databases/Assignment 1/source.txt"
    assert files[0]["download_url"].startswith("/api/files/download?")

    # The old bug re-listed the root at every depth, producing duplicates.
    assert "Databases" not in [child["name"] for child in databases["children"]]


def test_workspace_download_is_confined_to_the_jail(api) -> None:
    client, _state, settings = api

    (settings.workspace_dir / "notes.txt").write_text("course notes", encoding="utf-8")
    (settings.generated_dir / "answer.txt").write_text("generated", encoding="utf-8")
    (settings.project_root / "secret.txt").write_text("secret", encoding="utf-8")

    ok = client.get(
        "/api/files/download", params={"root": "workspace", "path": "notes.txt"}
    )
    assert ok.status_code == 200
    assert ok.text == "course notes"

    generated = client.get(
        "/api/files/download", params={"root": "generated", "path": "answer.txt"}
    )
    assert generated.status_code == 200
    assert generated.text == "generated"

    traversal = client.get(
        "/api/files/download", params={"root": "workspace", "path": "../secret.txt"}
    )
    assert traversal.status_code == 400

    absolute = client.get(
        "/api/files/download",
        params={"root": "workspace", "path": str(settings.project_root / "secret.txt")},
    )
    assert absolute.status_code == 400

    bad_root = client.get(
        "/api/files/download", params={"root": "project", "path": "secret.txt"}
    )
    assert bad_root.status_code == 422


def test_reset_endpoint_clears_data_and_keeps_login(api) -> None:
    client, state, settings = api

    course_id = state.db.upsert_course(
        "Databases", "https://moodle.example.edu/course/view.php?id=2"
    )
    state.db.upsert_assignment(
        course_id, "Lab 1", "https://moodle.example.edu/mod/assign/view.php?id=7"
    )
    conversation_id = state.db.create_conversation("Old chat")
    state.db.add_message(conversation_id, "user", "hello")
    state._moodle_url = "https://moodle.example.edu/"

    folder = settings.workspace_dir / "Databases" / "Lab 1"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "notes.txt").write_text("notes", encoding="utf-8")
    (settings.workspace_dir / ".gitkeep").write_text("", encoding="utf-8")
    # A stray placeholder inside a course folder must not keep that folder alive.
    stray = settings.workspace_dir / "Software Engineering"
    stray.mkdir(parents=True, exist_ok=True)
    (stray / ".gitkeep").write_text("", encoding="utf-8")

    blocked = client.post("/api/reset", json={})
    assert blocked.status_code == 403

    response = client.post("/api/reset", json={}, headers={"X-Owlet": "1"})
    assert response.status_code == 200
    summary = response.json()
    assert summary["ok"] is True
    assert summary["courses"] == 1
    assert summary["assignments"] == 1
    assert summary["conversations"] == 1
    assert summary["messages"] == 1
    assert summary["workspace_files"] >= 3  # notes.txt, Lab 1, Databases
    assert summary["login_kept"] is True

    assert client.get("/api/courses").json() == {"courses": []}
    assert client.get("/api/assignments").json() == {"assignments": []}
    assert state.db.list_conversations() == []

    # Only the reset itself is left in the activity log.
    activity = state.db.list_activity()
    assert [item["kind"] for item in activity] == ["reset"]

    # Placeholders and the saved Moodle URL survive, so no re-login is needed.
    assert (settings.workspace_dir / ".gitkeep").exists()
    assert not folder.exists()
    assert not stray.exists()
    assert client.get("/api/state").json()["moodle_url"] == "https://moodle.example.edu/"


def test_approval_and_conversation_errors_are_clear(api) -> None:
    client, state, _settings = api

    unknown = client.post(
        "/api/approvals/doesnotexist",
        json={"approved": True},
        headers={"X-Owlet": "1"},
    )
    assert unknown.status_code == 404

    missing_chat = client.get("/api/conversations/9999/messages")
    assert missing_chat.status_code == 404

    blank = client.post(
        "/api/chat",
        json={"message": "   "},
        headers={"X-Owlet": "1"},
    )
    assert blank.status_code == 400

    empty = client.post(
        "/api/chat",
        json={"message": ""},
        headers={"X-Owlet": "1"},
    )
    assert empty.status_code == 422

    conversation_id = state.db.create_conversation("Existing chat")
    state.db.add_message(conversation_id, "user", "hello")
    history = client.get(f"/api/conversations/{conversation_id}/messages")
    assert history.status_code == 200
    assert history.json()["messages"][0]["content"] == "hello"


def test_disconnected_browser_raises_actionable_tool_error(api) -> None:
    client, state, settings = api

    ctx = ToolContext(
        settings=settings,
        db=state.db,
        browser=state.browser,
        jail=state.jail,
    )

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(ctx.require_login())

    message = str(excinfo.value).lower()
    assert "browser is not connected" in message
    assert "click connect" in message


def test_chat_turn_completes_and_persists(api) -> None:
    client, state, _settings = api

    response = client.post(
        "/api/chat",
        json={"message": "Say hello"},
        headers={"X-Owlet": "1"},
    )
    assert response.status_code == 200
    body = response.json()
    conversation_id = body["conversation_id"]
    assert body["run_id"]

    deadline = time.time() + 5
    while time.time() < deadline:
        status = client.get("/api/agent/status").json()
        if not status["running"] and status["status"] == "idle":
            break
        time.sleep(0.05)

    assert client.get("/api/agent/status").json()["running"] is False
    history = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    assert [row["role"] for row in history] == ["user", "assistant"]
    assert history[-1]["content"] == "Hello from the fake model."

    second = client.post(
        "/api/chat",
        json={"message": "Again", "conversation_id": conversation_id},
        headers={"X-Owlet": "1"},
    )
    assert second.status_code == 200
    assert second.json()["conversation_id"] == conversation_id

    deadline = time.time() + 5
    while time.time() < deadline:
        if not state.agent_state()["running"]:
            break
        time.sleep(0.05)
    assert state.agent_state()["running"] is False


def test_chat_events_carry_stable_message_ids(api) -> None:
    client, state, _settings = api
    seen: list[tuple[str, str, dict | None]] = []
    original = state.emit

    def record(kind, message, data=None):
        seen.append((kind, message, data))
        original(kind, message, data)

    state.emit = record  # type: ignore[method-assign]
    try:
        response = client.post(
            "/api/chat",
            json={"message": "Say hello"},
            headers={"X-Owlet": "1"},
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]

        deadline = time.time() + 5
        while time.time() < deadline:
            if not state.agent_state()["running"]:
                break
            time.sleep(0.05)
    finally:
        state.emit = original  # type: ignore[method-assign]

    users = [data for kind, _msg, data in seen if kind == "user_message"]
    assert len(users) == 1
    assert users[0]["message_id"] == f"{run_id}:user"

    deltas = [data for kind, _msg, data in seen if kind == "assistant_delta"]
    finals = [data for kind, _msg, data in seen if kind == "assistant_message"]
    done = [data for kind, _msg, data in seen if kind == "done"]
    assert deltas and len(finals) == 1 and len(done) == 1
    assert len({data["message_id"] for data in deltas}) == 1
    streamed_id = deltas[0]["message_id"]
    assert finals[0]["message_id"] == streamed_id
    assert done[0]["message_id"] == streamed_id, "done must finalise the streamed bubble"


# ---------- chat images, regenerate, conversation management ----------

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG).decode("ascii")


def _wait_idle(client, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get("/api/agent/status").json()
        if not status["running"] and status["status"] == "idle":
            return
        time.sleep(0.05)
    raise AssertionError("The agent did not become idle in time.")


def test_image_only_chat_is_stored_served_and_sent_to_the_model(api) -> None:
    client, state, settings = api

    response = client.post(
        "/api/chat",
        json={
            "message": "",
            "images": [{"name": "photo.png", "mime": "image/png", "data": PNG_B64}],
        },
        headers={"X-Owlet": "1"},
    )
    assert response.status_code == 200
    conversation_id = response.json()["conversation_id"]
    _wait_idle(client)

    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    user_message = messages[0]
    # An image with no words still gets stable prompt text, for Regenerate.
    assert user_message["content"].startswith("Please look at the attached image")
    assert user_message["images"][0]["mime"] == "image/png"
    url = user_message["images"][0]["url"]
    assert url.startswith(f"/api/chat/images/{conversation_id}/")

    served = client.get(url)
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"
    assert served.content == PNG

    # The bytes reach the model as base64, on the newest user turn.
    assert state.llm.last_messages[-1]["role"] == "user"
    assert state.llm.last_messages[-1]["images"] == [PNG_B64]

    stored = settings.chat_images_dir / str(conversation_id)
    assert len(list(stored.glob("*.png"))) == 1


def test_invalid_or_excess_images_are_rejected_cleanly(api) -> None:
    client, state, settings = api

    not_an_image = base64.b64encode(b"this is text").decode("ascii")
    rejected = client.post(
        "/api/chat",
        json={"message": "look", "images": [{"name": "x.txt", "data": not_an_image}]},
        headers={"X-Owlet": "1"},
    )
    assert rejected.status_code == 400
    assert "PNG, JPEG, WebP, or GIF" in rejected.json()["detail"]

    # The failed first message must not leave an empty conversation behind.
    assert client.get("/api/conversations").json()["conversations"] == []
    assert list(settings.chat_images_dir.glob("*")) == []

    too_many = client.post(
        "/api/chat",
        json={
            "message": "look",
            "images": [
                {"name": f"{index}.png", "data": PNG_B64}
                for index in range(state.settings.max_images_per_message + 1)
            ],
        },
        headers={"X-Owlet": "1"},
    )
    assert too_many.status_code == 400
    assert "at most" in too_many.json()["detail"]

    assert client.get("/api/conversations").json()["conversations"] == []


def test_chat_image_route_refuses_anything_outside_its_folder(api) -> None:
    client, state, _settings = api

    conversation_id = state.db.create_conversation("Images")
    unknown = f"/api/chat/images/{conversation_id}/{'a' * 32}.png"
    assert client.get(unknown).status_code == 404

    traversal = client.get(
        f"/api/chat/images/{conversation_id}/..%2F..%2F..%2Fowlet.db"
    )
    assert traversal.status_code == 404

    wrong_name = client.get(f"/api/chat/images/{conversation_id}/photo.png")
    assert wrong_name.status_code == 404


def test_regenerate_replaces_the_last_answer_without_duplicating_the_question(api) -> None:
    client, state, _settings = api

    first = client.post(
        "/api/chat", json={"message": "Explain joins"}, headers={"X-Owlet": "1"}
    )
    conversation_id = first.json()["conversation_id"]
    _wait_idle(client)
    assert state.llm.calls == 1

    again = client.post(
        "/api/chat/regenerate",
        json={"conversation_id": conversation_id},
        headers={"X-Owlet": "1"},
    )
    assert again.status_code == 200
    assert again.json()["conversation_id"] == conversation_id
    _wait_idle(client)
    assert state.llm.calls == 2

    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    assert [row["role"] for row in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "Explain joins"
    assert messages[-1]["content"] == "Hello from the fake model."

    unknown = client.post(
        "/api/chat/regenerate",
        json={"conversation_id": 9999},
        headers={"X-Owlet": "1"},
    )
    assert unknown.status_code == 400


def test_conversations_can_be_renamed_and_deleted_with_their_images(api) -> None:
    client, state, settings = api

    created = client.post(
        "/api/chat",
        json={"message": "Look", "images": [{"name": "a.png", "data": PNG_B64}]},
        headers={"X-Owlet": "1"},
    )
    conversation_id = created.json()["conversation_id"]
    _wait_idle(client)
    image_url = client.get(f"/api/conversations/{conversation_id}/messages").json()[
        "messages"
    ][0]["images"][0]["url"]
    assert client.get(image_url).status_code == 200

    blocked = client.patch(
        f"/api/conversations/{conversation_id}", json={"title": "Renamed"}
    )
    assert blocked.status_code == 403

    renamed = client.patch(
        f"/api/conversations/{conversation_id}",
        json={"title": "  Renamed  chat "},
        headers={"X-Owlet": "1"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["conversation"]["title"] == "Renamed chat"

    blank = client.patch(
        f"/api/conversations/{conversation_id}",
        json={"title": "   "},
        headers={"X-Owlet": "1"},
    )
    assert blank.status_code == 400

    missing = client.patch(
        "/api/conversations/9999", json={"title": "Nope"}, headers={"X-Owlet": "1"}
    )
    assert missing.status_code == 404

    deleted = client.delete(
        f"/api/conversations/{conversation_id}", headers={"X-Owlet": "1"}
    )
    assert deleted.status_code == 200
    assert deleted.json()["images_removed"] == 1

    assert client.get(f"/api/conversations/{conversation_id}/messages").status_code == 404
    assert client.get(image_url).status_code == 404
    assert not (settings.chat_images_dir / str(conversation_id)).exists()
    assert state.db.list_conversations() == []

    # Deleting again is a clear 404, not a silent success.
    assert (
        client.delete(f"/api/conversations/{conversation_id}", headers={"X-Owlet": "1"})
        .status_code
        == 404
    )
