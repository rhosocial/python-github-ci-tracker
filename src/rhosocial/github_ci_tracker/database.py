# src/rhosocial/github_ci_tracker/database.py
"""The database seam: one pool per process, one connection per coroutine.

``Model.configure()`` is per-class: configuring six models would open six
connections, and a transaction opened through one model would not cover a
statement issued through another. The answer is the library's own -- an
:class:`AsyncBackendPool` whose ``connection()`` publishes the acquired backend
on a ``ContextVar`` that ``Model.backend()`` reads before falling back to
``__backend__``.

So this is an object, and the scope is a method on it:

    async with database.connection():
        ...  # every model resolves to the same connection here

Nothing here is a free function: the pool is the library's, this class adds the
scope and a short audit trail to it, and the maintenance connection (does the
database exist, create it, drop it) is a second, separate object.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from rhosocial.activerecord.backend.impl.postgres.backend import AsyncPostgresBackend
from rhosocial.activerecord.backend.impl.postgres.config import PostgresConnectionConfig
from rhosocial.activerecord.connection.pool import AsyncBackendPool, PoolConfig

from .config import Settings, get_settings
from .core.exceptions import ValidationError
from .credentials import PROVIDER_MODELS
from .models import (
    AsyncJob,
    AsyncJobLog,
    AsyncRepository,
    AsyncRepositoryBinding,
    AsyncStep,
    AsyncStepLog,
    AsyncWorkflow,
    AsyncWorkflowRun,
)

#: The session timezone every connection runs in. UTC, because that is the only
#: zone this service speaks: a column is ``timestamptz``, and a value comes back
#: in whatever zone the connection session uses -- which on a database the
#: operator did not configure is the server's local zone.
SET_TIME_ZONE = "SET TIME ZONE 'UTC'"

#: Every model bound inside a :meth:`Database.connection` scope: the async twins
#: the service actually uses. The sync twins exist for parity and stay unbound.
#:
#: Provider models are prepended from ``PROVIDER_MODELS`` rather than listed
#: here, because the core must not name a provider's models. A binding row and
#: the credential it names have to resolve inside one connection scope, so they
#: share this tuple.
MODELS: tuple[type, ...] = (
    *PROVIDER_MODELS,
    AsyncRepository,
    AsyncRepositoryBinding,
    AsyncWorkflow,
    AsyncWorkflowRun,
    AsyncJob,
    AsyncStep,
    AsyncStepLog,
    AsyncJobLog,
)


class Database:
    """The process's connection pool, and the coroutine scope over it.

    One instance per process. ``connection()`` is the only way a caller should
    reach the models: the pool's own context is what gives each task a
    connection of its own, and a model that resolves outside such a scope has no
    backend at all -- which is the correct answer, not an accident.
    """

    def __init__(self, pool: AsyncBackendPool) -> None:
        self._pool = pool

    @classmethod
    async def create(cls, settings: Settings | None = None) -> Database:
        """Build the pool this process will use."""
        resolved = settings or get_settings()
        pool = await AsyncBackendPool.create(
            PoolConfig(
                min_size=resolved.pool.min_size,
                max_size=resolved.pool.max_size,
                backend_factory=lambda: AsyncPostgresBackend(
                    connection_config=cls.connection_config(resolved)
                ),
            )
        )
        return cls(pool)

    @staticmethod
    def connection_config(settings: Settings) -> PostgresConnectionConfig:
        """The psycopg coordinates of one settings object."""
        db = settings.database
        return PostgresConnectionConfig(
            host=db.host,
            port=db.port,
            username=db.username,
            password=db.password,
            database=db.database,
            sslmode=db.sslmode,
        )

    @property
    def pool(self) -> AsyncBackendPool:
        """The underlying pool, for code that needs the library's own API."""
        return self._pool

    def connection(self) -> Any:
        """Make one pooled connection the scope's connection for every model.

        The binding is the pool's own connection context (a ``ContextVar``), not
        an assignment to the model classes. That distinction is the whole point:
        ``__backend__`` is a **class attribute**, so writing it per request lets
        two concurrent requests overwrite each other's connection and restore a
        stale value afterwards -- one request then writes through another
        request's connection.

        Nested scopes reuse the outer connection rather than acquiring a second,
        because the pool's own context is already a ``ContextVar`` and a second
        acquire would deadlock against a pool at ``max_size``.
        """
        return self._connection()

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[AsyncPostgresBackend]:
        async with self._pool.connection() as backend:
            # The session timezone belongs to the server, not to this service:
            # a database configured with Asia/Shanghai hands back
            # `2026-10-26T15:32:41+08:00` and every timestamp this service
            # publishes is supposed to be UTC with the offset stated. Pin it per
            # connection rather than normalising value by value -- a value read
            # through any path the views do not cover would otherwise leak the
            # server's zone to a caller that compares it against another UTC one.
            await backend.execute(SET_TIME_ZONE)
            yield backend

    async def ping(self, *, timeout: float = 2.0) -> None:
        """Prove the database answers one trivial query, or raise.

        This is the readiness check, so it is bounded and it is cheap: a probe
        that waits on the pool forever turns "the database is slow" into "the
        probe never returns", which reads as a dead process. The bound is also
        the honest answer to a saturated pool -- an instance that cannot get a
        connection within two seconds cannot serve a request either.

        The query is ``SELECT 1`` and nothing else. Whether the *schema* exists
        is a startup question (``missing_tables``), not one to ask every ten
        seconds; a reachable database with no tables is reported separately.

        Raises:
            TimeoutError: No connection and answer within ``timeout`` seconds.
            Exception: Whatever the driver raises -- unreachable host, refused
                credentials, a closed pool.
        """

        async def check() -> None:
            async with self._pool.connection() as backend:
                await backend.execute("SELECT 1")

        try:
            await asyncio.wait_for(check(), timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"no answer within {timeout:g}s") from exc

    async def close(self) -> None:
        """Close the pool and every connection in it."""
        if not self._pool.is_closed:
            await self._pool.close()


class PostgresServer:
    """The maintenance connection: does the database exist, create it, drop it.

    The service connects to one database; these questions are about the database
    itself, and PostgreSQL refuses ``CREATE``/``DROP DATABASE`` from a connection
    to the database in question -- so they go through a separate connection to a
    database that already exists.
    """

    #: The only database names this class interpolates into DDL. PostgreSQL has
    #: no bind parameters for CREATE DATABASE, so the name is validated first and
    #: the statement stays free of injection surfaces.
    NAME = re.compile(r"^[A-Za-z0-9_-]{1,63}$")

    def __init__(
        self,
        connection_config: PostgresConnectionConfig,
        maintenance_database: str = "postgres",
    ) -> None:
        self._config = connection_config
        self._maintenance_database = maintenance_database

    async def exists(self) -> bool:
        """Report whether the configured database exists, without creating it.

        The read goes through the maintenance connection: connecting to a
        database that does not exist fails at the driver, which cannot tell
        "absent" apart from "unreachable". ``is-initialized`` needs that
        distinction -- an absent database is a first boot, an unreachable one is
        an error.
        """
        name = self._checked_name()
        async with self._maintenance() as backend:
            return await self._exists_on(backend, name)

    async def ensure(self) -> bool:
        """Create the configured database when the server does not have it.

        A first deployment points at a database name that may not exist, and this
        project keeps no migrations: ``CREATE TABLE IF NOT EXISTS`` needs a
        database to run in. Creating it here makes a fresh deployment -- a
        container's first boot, a new environment -- converge with no manual
        step, while an existing database is left untouched.

        Returns:
            ``True`` when the database was created, ``False`` when it already
            existed.

        Raises:
            ValidationError: If the configured database name is not a safe
                identifier.
            CitrackerError: If the server is unreachable.
        """
        name = self._checked_name()
        async with self._maintenance() as backend:
            if await self._exists_on(backend, name):
                return False
            await backend.execute(f'CREATE DATABASE "{name}"')
            return True

    async def drop(self) -> bool:
        """Drop the configured database, terminating its connections first.

        The counterpart to :meth:`ensure`, for a deployment that is being torn
        down. Connections are terminated because PostgreSQL refuses to drop a
        database while another session is attached; ``pg_terminate_backend`` is
        the supported way to detach them.

        Returns:
            ``True`` when the database was dropped, ``False`` when it did not
            exist.

        Raises:
            ValidationError: If the configured database name is not a safe
                identifier, or names the maintenance database.
            CitrackerError: If the server is unreachable.
        """
        name = self._checked_name()
        if name == self._maintenance_database:
            raise ValidationError(
                f"Refusing to drop the maintenance database '{name}'; choose another "
                "CITRACKER_DB_NAME or --maintenance-db"
            )

        async with self._maintenance() as backend:
            if not await self._exists_on(backend, name):
                return False
            await backend.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{name}' AND pid <> pg_backend_pid()"
            )
            await backend.execute(f'DROP DATABASE "{name}"')
            return True

    @asynccontextmanager
    async def _maintenance(self) -> AsyncIterator[AsyncPostgresBackend]:
        """A connection to a database that already exists."""
        backend = AsyncPostgresBackend(
            connection_config=replace(self._config, database=self._maintenance_database)
        )
        await backend.connect()
        try:
            yield backend
        finally:
            await backend.disconnect()

    async def _exists_on(self, backend: AsyncPostgresBackend, name: str) -> bool:
        rows = await backend.execute(f"SELECT 1 FROM pg_database WHERE datname = '{name}'")
        return bool(rows.data)

    def _checked_name(self) -> str:
        """Validate the database name before it is interpolated into DDL."""
        name = self._config.database
        if not self.NAME.fullmatch(name):
            raise ValidationError(
                f"Database name '{name}' is not a valid identifier; use letters, "
                "digits, dash and underscore only"
            )
        return name


__all__ = ["MODELS", "Database", "PostgresServer"]
