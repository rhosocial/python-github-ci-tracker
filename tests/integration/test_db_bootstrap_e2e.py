# tests/integration/test_db_bootstrap_e2e.py
"""The self-sufficient database bootstrap, against a real PostgreSQL server.

Covers the path a container's first boot takes: the database does not exist yet,
so the bootstrap creates it, the schema is built inside it, a second run is a
no-op, and the test cleans up after itself. Gated like the other integration
module -- ``CITRACKER_E2E_POSTGRES=1`` with ``CITRACKER_E2E_DB_*`` -- so the
default suite stays serverless.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from typing import Any

import pytest
from rhosocial.activerecord.backend.impl.postgres.backend import AsyncPostgresBackend
from rhosocial.activerecord.backend.impl.postgres.config import PostgresConnectionConfig

from rhosocial.github_ci_tracker.database import PostgresServer
from rhosocial.github_ci_tracker.schema import create_all

REQUIRES_POSTGRES = "CITRACKER_E2E_POSTGRES"

pytestmark = pytest.mark.skipif(
    not os.getenv(REQUIRES_POSTGRES),
    reason=f"set {REQUIRES_POSTGRES}=1 with CITRACKER_E2E_DB_* to run this module",
)


def _db_settings() -> dict[str, Any]:
    """Read the E2E database coordinates, defaulting to the project ones."""
    return {
        "host": os.getenv("CITRACKER_E2E_DB_HOST", "192.168.1.3"),
        "port": int(os.getenv("CITRACKER_E2E_DB_PORT", "16689")),
        "username": os.getenv("CITRACKER_E2E_DB_USER", "root"),
        "password": os.getenv("CITRACKER_E2E_DB_PASSWORD", "password"),
        "database": os.getenv("CITRACKER_E2E_DB_NAME", "test_db"),
    }


def _run(coro: Any) -> Any:
    """Run a coroutine on a loop psycopg accepts."""
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            return runner.run(coro)
    return asyncio.run(coro)


def test_ensure_database_creates_builds_and_cleans_up() -> None:
    """create -> build the schema inside -> second run is a no-op -> drop."""
    settings = _db_settings()
    name = f"citracker_e2e_{uuid.uuid4().hex[:8]}"
    config = PostgresConnectionConfig(**{**settings, "database": name})

    async def scenario() -> None:
        created = await PostgresServer(config).ensure()
        assert created is True, "a fresh name must be created"

        # The schema builds inside the database that was just created.
        backend = AsyncPostgresBackend(connection_config=config)
        await backend.connect()
        try:
            await create_all(backend)
            rows = await backend.execute(
                "SELECT to_regclass('ct_repositories') IS NOT NULL AS table_exists"
            )
            row = rows.data[0] if rows.data else {}
            # Rows come back as tuples or mappings depending on the driver.
            table_exists = next(iter(row.values())) if isinstance(row, dict) else row[0]
            assert table_exists is True
        finally:
            await backend.disconnect()

        # Idempotent: the existing database is left alone.
        again = await PostgresServer(config).ensure()
        assert again is False

        # Cleanup through the maintenance connection -- a database cannot be
        # dropped while connected to it.
        maintenance = AsyncPostgresBackend(connection_config=PostgresConnectionConfig(**settings))
        await maintenance.connect()
        try:
            await maintenance.execute(f'DROP DATABASE "{name}"')
        finally:
            await maintenance.disconnect()

    try:
        _run(scenario())
    finally:
        _drop_if_present(settings, name)


def _drop_if_present(settings: dict[str, Any], name: str) -> None:
    """A failed test must not leave its database behind on the server."""

    async def scenario() -> None:
        backend = AsyncPostgresBackend(connection_config=PostgresConnectionConfig(**settings))
        await backend.connect()
        try:
            rows = await backend.execute(f"SELECT 1 FROM pg_database WHERE datname = '{name}'")
            if rows.data:
                await backend.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await backend.disconnect()

    _run(scenario())
