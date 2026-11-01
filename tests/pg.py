# tests/pg.py
"""Where a PostgreSQL test server comes from.

The rule: the **environment always wins over the file**. CI supplies a service
container through the same ``CITRACKER_DB_*`` variables the application reads, and
a stale file on a developer's disk must not redirect a CI run at a server that is
not there. A developer names a scenarios file -- the sibling
``python-activerecord-postgres`` repository ships one with a ``postgres_18``
entry -- or simply exports the variables.

The database is always ``citracker_test``, whatever a scenarios file says: the YAML
is shared with another project and names *its* database. Everything else (host,
port, user, password, sslmode) comes from the environment, or from the file when
the environment is silent.

This is a contract, not a location: nothing here knows which server exists.
``resolve_scenario()`` returns ``None`` when nothing names one, and the fixture in
``conftest.py`` turns that into a skip.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

#: The database this project's tests run in, on whichever server the environment
#: names. Constant on purpose: a second database name would split test state
#: across two places for no gain.
TEST_DATABASE = "citracker_test"

#: Where a scenarios file may be named. The second name is the sibling project's,
#: so one file serves both without either hard-coding the other's path.
SCENARIO_FILE_ENV = "CITRACKER_TEST_PG_SCENARIO_FILE"
SCENARIO_FILE_ALIAS = "POSTGRES_SCENARIOS_CONFIG_PATH"
#: Which scenario to read from that file.
SCENARIO_NAME_ENV = "CITRACKER_TEST_PG_SCENARIO"
SCENARIO_NAME = "postgres_18"
#: The conventional location, relative to this repository. Deliberately absent
#: from the tree: a committed scenario would make every run reach for a server
#: that may not exist, and a committed password is worse than no default.
SCENARIO_FILE = Path("tests") / "config" / "postgres_scenarios.yaml"

#: Connection field -> the environment variables that override it, first wins.
#: ``CITRACKER_DB_*`` is what the application reads, so a CI job that configures
#: the database once configures the tests too; ``PG*`` is the libpq convention.
FIELD_ENV: dict[str, tuple[str, ...]] = {
    "host": ("CITRACKER_DB_HOST", "PGHOST"),
    "port": ("CITRACKER_DB_PORT", "PGPORT"),
    "username": ("CITRACKER_DB_USER", "PGUSER"),
    "password": ("CITRACKER_DB_PASSWORD", "PGPASSWORD"),
    "sslmode": ("CITRACKER_DB_SSLMODE", "PGSSLMODE"),
}


def _repo_root() -> Path:
    """The repository root, from the location of this file."""
    return Path(__file__).resolve().parents[1]


def _expand(value: Any) -> Any:
    """Replace ``${VAR}`` with its environment value, the sibling's convention.

    It is what lets a scenarios file be shared without carrying a password: the
    file says which variable holds it.
    """
    if not isinstance(value, str) or "${" not in value:
        return value
    return re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), ""), value)


def _scenario_from_file() -> dict[str, Any] | None:
    """Read one scenario from a YAML file, or ``None`` when there is no file."""
    candidates = (
        os.getenv(SCENARIO_FILE_ENV),
        os.getenv(SCENARIO_FILE_ALIAS),
        str(_repo_root() / SCENARIO_FILE),
    )
    located = next((c for c in candidates if c and Path(c).is_file()), None)
    if located is None:
        return None

    path = Path(located)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    name = os.getenv(SCENARIO_NAME_ENV, SCENARIO_NAME)
    scenario = (data.get("scenarios") or {}).get(name)
    if not scenario:
        # A file that exists but lacks the scenario is a mistake, not a
        # configuration: it would otherwise read as "no server configured".
        raise ValueError(f"scenario '{name}' is not defined in {path}")

    return {key: _expand(value) for key, value in scenario.items() if key != "enabled"}


def _scenario_from_env() -> dict[str, Any]:
    """Read connection fields from the environment; empty when none are set."""
    found: dict[str, Any] = {}
    for field, names in FIELD_ENV.items():
        for name in names:
            value = os.getenv(name)
            if value:
                found[field] = value
                break
    return found


def resolve_scenario() -> dict[str, Any] | None:
    """Connection parameters: file underneath, environment on top.

    Returns:
        Keyword arguments for
        :class:`~rhosocial.activerecord.backend.impl.postgres.config.PostgresConnectionConfig`,
        minus ``database`` -- that is always :data:`TEST_DATABASE` and is added by
        the fixture. ``None`` when nothing in the environment names a server.
    """
    scenario: dict[str, Any] = _scenario_from_file() or {}
    scenario.update(_scenario_from_env())
    scenario.pop("database", None)

    if not scenario.get("host"):
        return None
    if "port" in scenario:
        scenario["port"] = int(scenario["port"])
    return scenario


__all__ = [
    "FIELD_ENV",
    "SCENARIO_FILE",
    "SCENARIO_FILE_ALIAS",
    "SCENARIO_FILE_ENV",
    "SCENARIO_NAME",
    "SCENARIO_NAME_ENV",
    "TEST_DATABASE",
    "resolve_scenario",
]
