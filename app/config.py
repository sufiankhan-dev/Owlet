"""Typed, validated application configuration.

All values come from environment variables (optionally loaded from ``.env``).
The Moodle account password enters the process only when the owner opts in with
``LMS_AUTO_LOGIN=true``; it stays in memory, is excluded from ``repr``, and is
never logged, stored in the database, or sent to the model.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# The one place the default model is named. Override it with OLLAMA_MODEL.
DEFAULT_OLLAMA_MODEL = "qwen3.5:9b"
load_dotenv(PROJECT_ROOT / ".env")

# Hostnames that mean "the machine this app runs on".
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Tailscale hands out addresses from these ranges, so a peer can be recognised
# from the literal address without trusting DNS. Anything else is off-limits.
TAILSCALE_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)


def is_tailscale_host(host: str) -> bool:
    """True for a Tailscale CGNAT/ULA address or a MagicDNS ``*.ts.net`` name."""

    name = host.strip().strip("[]").lower()
    if name.endswith(".ts.net"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return any(address in network for network in TAILSCALE_NETWORKS)


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _env_str(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = _env_str(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name, "true" if default else "false").lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


def _env_course_urls(name: str) -> frozenset[tuple[str, str]]:
    """Parse comma-separated Moodle course URLs into (host, course id) pairs."""

    pairs: set[tuple[str, str]] = set()
    for raw in _env_str(name, "").split(","):
        url = raw.strip()
        if not url:
            continue
        parsed = urlparse(url)
        course_id = parse_qs(parsed.query).get("id", [""])[0].strip()
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or "/course/view.php" not in parsed.path.lower()
            or not course_id
        ):
            raise ValueError(
                f"{name} entries must be Moodle course URLs such as "
                f"https://moodle.example.edu/course/view.php?id=123, got {url!r}"
            )
        pairs.add((parsed.netloc.lower(), course_id))
    return frozenset(pairs)


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings for the whole application."""

    project_root: Path
    app_host: str
    app_port: int
    log_level: str

    ollama_host: str
    # True when the owner opted into a Tailscale peer via OLLAMA_ALLOW_REMOTE.
    ollama_allow_remote: bool
    ollama_model: str
    ollama_num_ctx: int
    ollama_temperature: float
    ollama_timeout_s: int
    ollama_keep_alive: str
    ollama_think: bool

    browser_channel: str
    browser_headless: bool
    browser_timeout_ms: int
    browser_profile_dir: Path
    moodle_url: str | None
    # Courses hidden everywhere, as (host, Moodle course id) from IGNORED_COURSES.
    ignored_courses: frozenset[tuple[str, str]]
    # Opt-in automatic Moodle account login (see AGENTS.md §1). Off by default;
    # when off these values are ignored and login stays manual.
    lms_auto_login: bool
    lms_username: str
    lms_password: str = field(repr=False)

    max_download_mb: int
    # Chat image attachments: per-image size cap and how many one message may carry.
    max_image_mb: int
    max_images_per_message: int
    max_page_chars: int
    max_tool_result_chars: int
    approval_timeout_s: int
    max_agent_iterations: int

    # Course/assignment sync. The dashboard mirrors submission status and due
    # dates from Moodle; these values decide when a due date starts shouting.
    sync_on_connect: bool
    due_soon_hours: int
    due_urgent_hours: int

    @property
    def app_dir(self) -> Path:
        return self.project_root / "app"

    @property
    def ui_dir(self) -> Path:
        return self.app_dir / "ui"

    @property
    def workspace_dir(self) -> Path:
        return self.project_root / "workspace"

    @property
    def generated_dir(self) -> Path:
        return self.project_root / "generated"

    @property
    def database_dir(self) -> Path:
        return self.project_root / "database"

    @property
    def database_path(self) -> Path:
        return self.database_dir / "owlet.db"

    @property
    def logs_dir(self) -> Path:
        return self.project_root / "logs"

    @property
    def chat_images_dir(self) -> Path:
        """Attached chat images, kept outside the user's workspace.

        The database stores only a reference; the bytes live here so SQLite
        stays small and history can be re-rendered after a reload.
        """
        return self.database_dir / "chat_images"

    @property
    def config_dir(self) -> Path:
        return self.project_root / "config"

    @property
    def max_download_bytes(self) -> int:
        return self.max_download_mb * 1024 * 1024

    @property
    def max_image_bytes(self) -> int:
        return self.max_image_mb * 1024 * 1024

    @property
    def ollama_is_remote(self) -> bool:
        """True when the model runs on a Tailscale peer instead of this machine."""

        return (urlparse(self.ollama_host).hostname or "") not in LOOPBACK_HOSTS

    @property
    def lms_credentials(self) -> tuple[str, str] | None:
        """The opt-in Moodle account pair, or None when login stays manual."""

        if self.lms_auto_login and self.lms_username and self.lms_password:
            return self.lms_username, self.lms_password
        return None

    @property
    def moodle_host(self) -> str | None:
        if not self.moodle_url:
            return None
        host = urlparse(self.moodle_url).hostname
        return host.lower() if host else None

    def ensure_directories(self) -> None:
        """Create every directory the app writes to. Idempotent."""
        for path in (
            self.workspace_dir,
            self.generated_dir,
            self.database_dir,
            self.chat_images_dir,
            self.logs_dir,
            self.browser_profile_dir,
            self.config_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    """Build settings from the environment, validating safety-critical values."""
    host = _env_str("APP_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "APP_HOST must stay on loopback (127.0.0.1/localhost). "
            "This app must never be exposed to the network."
        )

    ollama_host = _env_str("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    ollama_allow_remote = _env_bool("OLLAMA_ALLOW_REMOTE", False)
    parsed = urlparse(ollama_host)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"OLLAMA_HOST is not a valid http(s) URL: {ollama_host!r}")
    if parsed.hostname not in LOOPBACK_HOSTS:
        if not ollama_allow_remote:
            raise ValueError(
                f"OLLAMA_HOST points at {parsed.hostname!r}, which is not this machine. "
                "Set OLLAMA_ALLOW_REMOTE=true to use a Tailscale peer "
                "(100.64.0.0/10 or *.ts.net). Cloud inference is unsupported."
            )
        if not is_tailscale_host(parsed.hostname):
            raise ValueError(
                f"OLLAMA_HOST {parsed.hostname!r} is not a Tailscale address. "
                "OLLAMA_ALLOW_REMOTE=true admits only 100.64.0.0/10, "
                "fd7a:115c:a1e0::/48, and *.ts.net hosts; "
                "public and LAN endpoints stay refused."
            )

    moodle_url = _env_str("MOODLE_URL", "") or None
    if moodle_url:
        parsed_moodle = urlparse(moodle_url)
        if parsed_moodle.scheme not in {"http", "https"} or not parsed_moodle.hostname:
            raise ValueError(f"MOODLE_URL must be an http(s) URL, got {moodle_url!r}")

    lms_auto_login = _env_bool("LMS_AUTO_LOGIN", False)
    lms_username = _env_str("LMS_USERNAME", "")
    # Passwords may contain leading/trailing spaces, so never strip this one.
    lms_password = os.getenv("LMS_PASSWORD", "") or ""
    if lms_auto_login and not (lms_username and lms_password):
        raise ValueError(
            "LMS_AUTO_LOGIN=true needs both LMS_USERNAME and LMS_PASSWORD in .env. "
            "Set them, or remove the flag and log in manually in Chrome."
        )
    if not lms_auto_login:
        # Keep the secret out of the process entirely unless it is opted in.
        lms_username = ""
        lms_password = ""

    profile = Path(_env_str("BROWSER_PROFILE_DIR", "browser/profile"))
    if not profile.is_absolute():
        profile = PROJECT_ROOT / profile

    channel = _env_str("BROWSER_CHANNEL", "chrome")
    if channel not in {"chrome", "msedge", "chromium"}:
        raise ValueError("BROWSER_CHANNEL must be chrome, msedge, or chromium")

    due_urgent_hours = _env_int("DUE_URGENT_HOURS", 24, 1, 720)
    due_soon_hours = _env_int("DUE_SOON_HOURS", 48, 1, 720)
    if due_urgent_hours > due_soon_hours:
        raise ValueError(
            "DUE_URGENT_HOURS must be less than or equal to DUE_SOON_HOURS, "
            f"got {due_urgent_hours} and {due_soon_hours}"
        )

    settings = Settings(
        project_root=PROJECT_ROOT,
        app_host=host,
        app_port=_env_int("APP_PORT", 8765, 1024, 65535),
        log_level=_env_str("LOG_LEVEL", "INFO").upper(),
        ollama_host=ollama_host,
        ollama_allow_remote=ollama_allow_remote,
        ollama_model=_env_str("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
        ollama_num_ctx=_env_int("OLLAMA_NUM_CTX", 16384, 2048, 65536),
        ollama_temperature=_env_float("OLLAMA_TEMPERATURE", 0.2, 0.0, 2.0),
        ollama_timeout_s=_env_int("OLLAMA_TIMEOUT_S", 600, 30, 3600),
        ollama_keep_alive=_env_str("OLLAMA_KEEP_ALIVE", "30m"),
        ollama_think=_env_bool("OLLAMA_THINK", False),
        browser_channel=channel,
        browser_headless=_env_bool("BROWSER_HEADLESS", False),
        browser_timeout_ms=_env_int("BROWSER_TIMEOUT_MS", 30000, 5000, 180000),
        browser_profile_dir=profile,
        moodle_url=moodle_url,
        ignored_courses=_env_course_urls("IGNORED_COURSES"),
        lms_auto_login=lms_auto_login,
        lms_username=lms_username,
        lms_password=lms_password,
        max_download_mb=_env_int("MAX_DOWNLOAD_MB", 50, 1, 500),
        max_image_mb=_env_int("MAX_IMAGE_MB", 8, 1, 20),
        max_images_per_message=_env_int("MAX_IMAGES_PER_MESSAGE", 4, 1, 10),
        max_page_chars=_env_int("MAX_PAGE_CHARS", 14000, 2000, 100000),
        max_tool_result_chars=_env_int("MAX_TOOL_RESULT_CHARS", 8000, 1000, 50000),
        approval_timeout_s=_env_int("APPROVAL_TIMEOUT_S", 300, 30, 3600),
        max_agent_iterations=_env_int("MAX_AGENT_ITERATIONS", 20, 1, 50),
        sync_on_connect=_env_bool("SYNC_ON_CONNECT", True),
        due_soon_hours=due_soon_hours,
        due_urgent_hours=due_urgent_hours,
    )
    settings.ensure_directories()
    return settings
