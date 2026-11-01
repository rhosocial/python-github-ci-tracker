# src/rhosocial/github_ci_tracker/config.py
"""Service configuration.

Settings are read once per process from environment variables; every module
that needs a value goes through :func:`get_settings` instead of reading
``os.environ`` itself, so the vocabulary of variables lives in exactly one
place.

The database defaults match the ``postgres_18`` scenario of
``python-activerecord-postgres/tests/config/postgres_scenarios.yaml``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _load_dotenv() -> None:
    """Load the project ``.env`` into the process environment.

    Existing environment variables are never overwritten, so a value exported
    in the shell still wins over the file. This is what makes ``.env.example``
    operational rather than documentation: copy it to ``.env`` and the service
    picks the values up without any export step.
    """
    root = Path(__file__).resolve().parents[4]
    for candidate in (root / ".env", Path.cwd() / ".env"):
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _int_env(name: str, default: int) -> int:
    """Read an integer environment variable, falling back on a default."""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    """Read a float environment variable, falling back on a default."""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class DatabaseSettings:
    """PostgreSQL connection settings."""

    host: str = "192.168.1.3"
    port: int = 16689
    username: str = "root"
    password: str = "password"
    database: str = "test_db"
    sslmode: str = "prefer"

    @classmethod
    def from_env(cls) -> DatabaseSettings:
        """Build settings from the ``CITRACKER_DB_*`` variables."""
        return cls(
            host=os.getenv("CITRACKER_DB_HOST", "192.168.1.3"),
            port=_int_env("CITRACKER_DB_PORT", 16689),
            username=os.getenv("CITRACKER_DB_USER", "root"),
            password=os.getenv("CITRACKER_DB_PASSWORD", "password"),
            database=os.getenv("CITRACKER_DB_NAME", "test_db"),
            sslmode=os.getenv("CITRACKER_DB_SSLMODE", "prefer"),
        )


@dataclass(frozen=True)
class PoolSettings:
    """Connection pool sizing."""

    min_size: int = 1
    max_size: int = 10

    @classmethod
    def from_env(cls) -> PoolSettings:
        """Build settings from the ``CITRACKER_POOL_*`` variables."""
        return cls(
            min_size=_int_env("CITRACKER_POOL_MIN", 1),
            max_size=_int_env("CITRACKER_POOL_MAX", 10),
        )


@dataclass(frozen=True)
class LogSettings:
    """Step-log capture.

    GitHub keeps a run's per-step log files for roughly an hour after the run
    finishes, then serves a flat archive instead. That hour is a *fetching*
    deadline, and the only thing this window controls: a log that was fetched is
    stored like any other row and never expires. It is a setting rather than a
    constant because it was measured, not documented, and because a deployment
    that cares about the storage cost can turn capture off entirely.
    """

    #: How long after completion a capture is still worth attempting.
    capture_window_minutes: int = 60
    #: Whether completed runs have their step files captured at all. Off means
    #: every log read uses the job-level fallback, which is the old behaviour.
    capture_enabled: bool = True
    #: How many times one capture may re-fetch the archive while steps that the
    #: job metadata says ran still have no file. GitHub writes those files
    #: asynchronously: a job's steps are final at completion, but the archive can
    #: lag by minutes, and nothing in the response says "not yet".
    capture_retry_attempts: int = 6
    #: Seconds between those attempts. Together with the attempt count this is
    #: the "wait for the archive to catch up" budget.
    capture_retry_delay_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> LogSettings:
        """Build settings from the ``CITRACKER_LOG_*`` variables."""
        flag = os.getenv("CITRACKER_LOG_CAPTURE", "1").strip().lower()
        return cls(
            capture_window_minutes=_int_env("CITRACKER_LOG_CAPTURE_WINDOW_MINUTES", 60),
            capture_enabled=flag not in {"0", "false", "no", "off"},
            capture_retry_attempts=_int_env("CITRACKER_LOG_CAPTURE_RETRIES", 6),
            capture_retry_delay_seconds=_float_env(
                "CITRACKER_LOG_CAPTURE_RETRY_DELAY_SECONDS", 60.0
            ),
        )


@dataclass(frozen=True)
class ServiceSettings:
    """Endpoint and secret settings."""

    rest_host: str = "0.0.0.0"
    rest_port: int = 8000
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8001
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 18080
    api_key: str = ""
    webhook_url: str = ""
    webhook_secret: str = ""
    #: TLS material for the webhook listener. GitHub requires an https callback
    #: URL with a publicly trusted certificate, so the receiver terminates TLS
    #: itself: a full-chain PEM (leaf first, then intermediates) plus its private
    #: key. Empty means plain HTTP, which GitHub refuses to register.
    webhook_ssl_certfile: str = ""
    webhook_ssl_keyfile: str = ""
    default_events: list[str] = field(default_factory=lambda: ["workflow_run", "workflow_job"])
    log_level: str = "INFO"
    #: Upper bound on one ``wait_for_run`` call, in seconds. The cap used to be
    #: a hard 50s, derived from the ~60s ceiling of the MCP clients that
    #: existed then; a client that sets a longer execution timeout (OpenCode's
    #: default is 12 hours) can now carry a 5-10 minute CI run in one call
    #: instead of ten. It stays configurable because raising it is only safe
    #: while the *client* tolerates a long request.
    wait_max_seconds: float = 300.0
    #: Alias of the fine-grained PAT the whole process uses. The credential is
    #: resolved once at startup and handed to every controller, so rotating it
    #: requires a restart; an empty/unregistered name leaves the controllers
    #: resolving per repository as before. The compose file sources this from
    #: ``CITRACKER_BOOTSTRAP_PAT_NAME`` so the first-boot script and the runtime
    #: cannot name different rows.
    pat_name: str = "default"

    @classmethod
    def from_env(cls) -> ServiceSettings:
        """Build settings from the ``CITRACKER_*`` variables."""
        events = os.getenv("CITRACKER_DEFAULT_EVENTS", "")
        return cls(
            rest_host=os.getenv("CITRACKER_REST_HOST", "0.0.0.0"),
            rest_port=_int_env("CITRACKER_REST_PORT", 8000),
            mcp_host=os.getenv("CITRACKER_MCP_HOST", "0.0.0.0"),
            mcp_port=_int_env("CITRACKER_MCP_PORT", 8001),
            webhook_host=os.getenv("CITRACKER_WEBHOOK_HOST", "0.0.0.0"),
            webhook_port=_int_env("CITRACKER_WEBHOOK_PORT", 18080),
            api_key=os.getenv("CITRACKER_API_KEY", ""),
            webhook_url=os.getenv("CITRACKER_WEBHOOK_URL", ""),
            webhook_secret=os.getenv("CITRACKER_WEBHOOK_SECRET", ""),
            webhook_ssl_certfile=os.getenv("CITRACKER_WEBHOOK_SSL_CERTFILE", ""),
            webhook_ssl_keyfile=os.getenv("CITRACKER_WEBHOOK_SSL_KEYFILE", ""),
            default_events=(
                [e.strip() for e in events.split(",") if e.strip()]
                if events
                else ["workflow_run", "workflow_job"]
            ),
            log_level=os.getenv("CITRACKER_LOG_LEVEL", "INFO"),
            wait_max_seconds=_float_env("CITRACKER_WAIT_MAX_SECONDS", 300.0),
            pat_name=os.getenv("CITRACKER_PAT_NAME", "default"),
        )


@dataclass(frozen=True)
class Settings:
    """All service settings, assembled once per process."""

    database: DatabaseSettings = field(default_factory=DatabaseSettings.from_env)
    pool: PoolSettings = field(default_factory=PoolSettings.from_env)
    service: ServiceSettings = field(default_factory=ServiceSettings.from_env)
    logs: LogSettings = field(default_factory=LogSettings.from_env)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings instance for this process."""
    _load_dotenv()
    return Settings()
