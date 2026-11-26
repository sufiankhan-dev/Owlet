"""Configuration validation, focused on the local/remote Ollama boundary."""

from __future__ import annotations

import pytest

from app.config import load_settings


def _configure(monkeypatch: pytest.MonkeyPatch, host: str, *, allow_remote: bool | None) -> None:
    """Point OLLAMA_HOST at ``host``; ``None`` leaves the opt-in flag unset."""

    monkeypatch.setenv("OLLAMA_HOST", host)
    if allow_remote is None:
        monkeypatch.delenv("OLLAMA_ALLOW_REMOTE", raising=False)
    else:
        monkeypatch.setenv("OLLAMA_ALLOW_REMOTE", "true" if allow_remote else "false")


@pytest.mark.parametrize(
    "host",
    ["http://localhost:11434", "http://127.0.0.1:11434", "http://[::1]:11434"],
)
def test_loopback_hosts_need_no_opt_in(monkeypatch, host: str) -> None:
    _configure(monkeypatch, host, allow_remote=None)

    settings = load_settings()

    assert settings.ollama_is_remote is False
    assert settings.ollama_allow_remote is False


def test_remote_host_is_refused_by_default(monkeypatch) -> None:
    _configure(monkeypatch, "http://100.101.102.103:11434", allow_remote=None)

    with pytest.raises(ValueError, match="OLLAMA_ALLOW_REMOTE"):
        load_settings()


def test_remote_host_is_refused_when_the_flag_is_false(monkeypatch) -> None:
    _configure(monkeypatch, "http://100.101.102.103:11434", allow_remote=False)

    with pytest.raises(ValueError, match="not this machine"):
        load_settings()


@pytest.mark.parametrize(
    "host",
    [
        "http://100.101.102.103:11434",
        "http://100.64.0.1:11434",
        "http://[fd7a:115c:a1e0::1]:11434",
        "http://friend-laptop.tailnet-abc.ts.net:11434",
    ],
)
def test_tailscale_hosts_are_accepted_when_opted_in(monkeypatch, host: str) -> None:
    _configure(monkeypatch, host, allow_remote=True)

    settings = load_settings()

    assert settings.ollama_is_remote is True
    assert settings.ollama_allow_remote is True


@pytest.mark.parametrize(
    "host",
    [
        "http://8.8.8.8:11434",
        "http://api.example.com:11434",
        "http://100.128.0.1:11434",
        "http://192.168.1.20:11434",
        "http://172.16.0.5:11434",
        "http://100.200.0.1.evil.example:11434",
    ],
)
def test_the_opt_in_does_not_admit_public_or_lan_endpoints(monkeypatch, host: str) -> None:
    _configure(monkeypatch, host, allow_remote=True)

    with pytest.raises(ValueError, match="not a Tailscale address"):
        load_settings()


def test_the_opt_in_is_harmless_for_a_local_daemon(monkeypatch) -> None:
    _configure(monkeypatch, "http://localhost:11434", allow_remote=True)

    settings = load_settings()

    assert settings.ollama_allow_remote is True
    assert settings.ollama_is_remote is False


def test_a_malformed_host_is_still_refused(monkeypatch) -> None:
    _configure(monkeypatch, "not-a-url", allow_remote=True)

    with pytest.raises(ValueError, match="not a valid http"):
        load_settings()


def _configure_lms(
    monkeypatch: pytest.MonkeyPatch,
    *,
    flag: str | None,
    username: str | None,
    password: str | None,
) -> None:
    """Set the opt-in Moodle login variables; ``None`` removes one."""

    for name, value in (
        ("LMS_AUTO_LOGIN", flag),
        ("LMS_USERNAME", username),
        ("LMS_PASSWORD", password),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_automatic_login_is_off_by_default(monkeypatch) -> None:
    _configure_lms(monkeypatch, flag=None, username="student", password="secret")

    settings = load_settings()

    assert settings.lms_auto_login is False
    assert settings.lms_credentials is None
    # The secret must not even be retained when the flag is off.
    assert settings.lms_username == ""
    assert settings.lms_password == ""


def test_automatic_login_needs_both_credentials(monkeypatch) -> None:
    _configure_lms(monkeypatch, flag="true", username="student", password=None)

    with pytest.raises(ValueError, match="LMS_USERNAME and LMS_PASSWORD"):
        load_settings()


def test_automatic_login_keeps_the_password_out_of_repr(monkeypatch) -> None:
    _configure_lms(monkeypatch, flag="true", username="student", password=" p@ss word ")

    settings = load_settings()

    # Surrounding spaces are part of a password and must survive.
    assert settings.lms_credentials == ("student", " p@ss word ")
    assert "p@ss word" not in repr(settings)

