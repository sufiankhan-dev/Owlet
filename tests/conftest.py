"""Pytest bootstrap: make the project root importable without installing it."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def isolate_model_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the developer's ``.env`` from deciding what the suite sees.

    ``load_settings()`` reads the process environment, so a laptop configured to
    use a Tailscale peer would otherwise fail every test that builds settings.
    Tests that care about these values set their own.
    """

    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    monkeypatch.delenv("OLLAMA_ALLOW_REMOTE", raising=False)
