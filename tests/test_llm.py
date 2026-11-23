"""Local-model plumbing: name matching, capabilities, and health messages."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from agent.llm import LLMError, OllamaService, has_image_content, model_matches
from app.config import load_settings


def test_model_matches_is_tag_aware() -> None:
    assert model_matches("qwen3.5:9b", "qwen3.5:9b")
    assert model_matches("qwen3.5:9b", "Qwen3.5:9B")
    assert model_matches("qwen3.5:9b", "qwen3.5:9b-q4_K_M")
    assert model_matches("qwen3.5", "qwen3.5:latest")

    # A different tag or a different family is not the configured model.
    assert not model_matches("qwen3.5:9b", "qwen3:8b")
    assert not model_matches("qwen3.5:9b", "gemma3:4b")
    assert not model_matches("qwen3.5:9b", "qwen3.5:4b")
    assert not model_matches("", "qwen3.5:9b")
    assert not model_matches("qwen3.5:9b", "")


def test_has_image_content_only_looks_at_user_payloads() -> None:
    assert has_image_content([{"role": "user", "content": "hi", "images": ["abc"]}])
    assert not has_image_content([{"role": "user", "content": "hi"}])
    assert not has_image_content([{"role": "assistant", "content": "hi", "images": []}])


class _StubClient:
    """Stands in for the Ollama SDK client; no network, no sockets."""

    def __init__(self, models, capabilities=None) -> None:
        self._models = models
        self._capabilities = capabilities or []

    async def list(self):
        return SimpleNamespace(
            models=[
                SimpleNamespace(
                    name=item["name"],
                    size=item.get("size", 0),
                    modified_at=item.get("modified_at", ""),
                )
                for item in self._models
            ]
        )

    async def ps(self):
        return SimpleNamespace(models=[])

    async def show(self, _model):
        return SimpleNamespace(capabilities=self._capabilities)


def _service(tmp_path, model: str) -> OllamaService:
    settings = replace(
        load_settings(),
        project_root=tmp_path,
        browser_profile_dir=tmp_path / "profile",
        ollama_model=model,
        ollama_host="http://127.0.0.1:11434",
    )
    return OllamaService(settings)


def test_health_reports_a_missing_model_with_the_exact_pull_command(tmp_path) -> None:
    service = _service(tmp_path, "qwen3.5:9b")
    service._client = _StubClient([{"name": "qwen3:8b", "size": 1, "modified_at": ""}])

    health = asyncio.run(service.health())

    assert health["reachable"] is True
    assert health["installed"] is False
    assert "ollama pull qwen3.5:9b" in health["error"]
    assert health["vision"] is None


def test_health_exposes_vision_and_thinking_capabilities(tmp_path) -> None:
    service = _service(tmp_path, "qwen3.5:9b")
    service._client = _StubClient(
        [{"name": "qwen3.5:9b-q4_K_M", "size": 1, "modified_at": ""}],
        capabilities=["completion", "tools", "vision", "thinking"],
    )

    health = asyncio.run(service.health())

    assert health["ok"] is True
    assert health["installed"] is True
    assert health["vision"] is True
    assert health["thinking"] is True
    assert health["error"] is None


def test_health_is_offline_when_the_daemon_cannot_be_reached(tmp_path) -> None:
    service = _service(tmp_path, "qwen3.5:9b")

    async def unreachable():
        raise LLMError("Cannot reach Ollama.", kind="connection")

    service.list_models = unreachable  # type: ignore[method-assign]

    health = asyncio.run(service.health())
    assert health["reachable"] is False
    assert health["installed"] is False
    assert health["error_kind"] == "connection"
    assert health["error"] == "Cannot reach Ollama."


def test_health_says_whether_the_model_runs_here_or_on_a_peer(tmp_path) -> None:
    local = _service(tmp_path, "qwen3.5:9b")
    local._client = _StubClient([{"name": "qwen3.5:9b"}])

    remote = OllamaService(
        replace(
            local.settings,
            ollama_host="http://100.88.56.96:11434",
            ollama_allow_remote=True,
        )
    )
    remote._client = _StubClient([{"name": "qwen3.5:9b"}])

    assert asyncio.run(local.health())["remote"] is False
    assert asyncio.run(remote.health())["remote"] is True
