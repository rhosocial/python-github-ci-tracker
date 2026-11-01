# tests/app/test_ensure_database.py
"""The database bootstrap guard, without touching a server.

``ensure_database`` interpolates the database name into DDL, because PostgreSQL
has no bind parameters for ``CREATE DATABASE``. That makes the name validation
the security boundary, and these cases pin it: no server is contacted when the
name is refused, and the exception is the one a caller can act on.

"Reaches the connection" used to be proved by pointing at a closed port and
asserting *some* exception. That is not a portable failure: on Windows the SYN is
dropped rather than refused, so the connect backs off for tens of seconds -- on
CI it passes instantly, on a developer's machine it hangs. The connection is
stubbed instead, so the test answers the question it was actually asking.
"""

from __future__ import annotations

import pytest
from rhosocial.activerecord.backend.impl.postgres.backend import AsyncPostgresBackend
from rhosocial.activerecord.backend.impl.postgres.config import PostgresConnectionConfig

from rhosocial.github_ci_tracker.core.exceptions import ValidationError
from rhosocial.github_ci_tracker.database import PostgresServer


def _config(database: str) -> PostgresConnectionConfig:
    return PostgresConnectionConfig(
        host="127.0.0.1",
        port=1,
        username="user",
        password="password",
        database=database,
    )


async def _refuse(*args: object, **kwargs: object) -> None:
    """Stand in for the connection: reachable proof, no network."""
    raise ConnectionRefusedError("stubbed by the test")


@pytest.mark.parametrize(
    "database",
    [
        'citracker"; DROP DATABASE postgres; --',
        "citracker WITH OWNER",
        "cit tracker",
        "citracker.",
        "",
        "x" * 64,
    ],
)
async def test_an_unsafe_database_name_is_refused(database: str, monkeypatch) -> None:
    """Injection shapes never reach the connection attempt."""
    monkeypatch.setattr(AsyncPostgresBackend, "connect", _refuse)

    with pytest.raises(ValidationError):
        await PostgresServer(_config(database)).ensure()


@pytest.mark.parametrize(
    "database",
    ["citracker", "citracker_e2e", "Cit-Tracker_2", "x" * 63],
)
async def test_a_plain_database_name_is_accepted(database: str, monkeypatch) -> None:
    """The shapes PostgreSQL allows pass validation and proceed to connect.

    The stub standing in for the connection is the proof: reaching it at all means
    the name passed the guard.
    """
    monkeypatch.setattr(AsyncPostgresBackend, "connect", _refuse)

    with pytest.raises(ConnectionRefusedError):
        await PostgresServer(_config(database)).ensure()
