# tests/adapters/test_mcp_database.py
"""The MCP listener must carry its own database.

``serve mcp`` is a separate process from the REST listener, so it inherits no
pool and has no request dependency where a connection could be bound. Before
this layer existed every tool called the business core with nothing bound and
answered ``No backend configured``. These tests pin the three properties that
fix depends on: a connection is bound for the duration of a request and
released afterwards, the pool is opened once rather than per request, and
shutdown closes it.
"""

from __future__ import annotations

import asyncio
import importlib
from contextlib import asynccontextmanager
from typing import Any

from rhosocial.github_ci_tracker.adapters.mcp.server import (
    _DatabaseBoundApp,
    _UnboundContainer,
)
from rhosocial.github_ci_tracker.database import MODELS

# The adapter class it replaces: imported by dotted path so monkeypatch
# replaces the classmethod the adapter actually calls.
db_module = importlib.import_module("rhosocial.github_ci_tracker.database")


class _FakeDatabase:
    """A database seam that publishes its connection the way the real one does.

    The binding is the pool's own connection context, so the fake has to set the
    same ContextVar; otherwise the test would pass on the class attribute alone
    and would not notice the attribute being (correctly) left alone.
    """

    def __init__(self) -> None:
        self.opened = 0
        self.closed = 0
        self.backend = object()

    @asynccontextmanager
    async def connection(self):
        from rhosocial.activerecord.connection.pool import context as pool_context

        token = pool_context._set_async_connection_backend(self.backend)
        try:
            yield self.backend
        finally:
            pool_context._reset_async_connection_backend(token)

    async def close(self) -> None:
        self.closed += 1


def _http_scope() -> dict[str, Any]:
    return {"type": "http", "method": "POST", "path": "/mcp", "headers": []}


async def _receive() -> dict[str, Any]:
    return {"type": "http.request"}


async def _send(message: dict[str, Any]) -> None:
    return None


def _patch_database(monkeypatch, database: _FakeDatabase) -> None:
    async def fake_create(cls, settings: Any = None) -> _FakeDatabase:
        database.opened += 1
        return database

    monkeypatch.setattr(db_module.Database, "create", classmethod(fake_create))


def test_a_request_binds_the_pool_connection_without_touching_class_attributes(
    monkeypatch,
) -> None:
    """The connection comes from the pool's context, not from ``__backend__``.

    Writing ``__backend__`` per request is what let two concurrent requests
    overwrite each other's connection: it is a class attribute, so the restore
    in one request can put another request's connection (or a released one) back
    in place.
    """
    database = _FakeDatabase()
    _patch_database(monkeypatch, database)
    observed: list[Any] = []
    before = [model.__backend__ for model in MODELS]

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        observed.append([model.backend() for model in MODELS])
        observed.append([model.__backend__ for model in MODELS])

    app = _DatabaseBoundApp(inner, _UnboundContainer())
    asyncio.run(app(_http_scope(), _receive, _send))

    assert observed, "the inner app should have been called"
    resolved, class_bindings = observed
    assert all(backend is database.backend for backend in resolved)
    assert class_bindings == before, "the class attributes must not be written"
    assert [model.__backend__ for model in MODELS] == before


def test_pool_is_opened_once_for_many_requests(monkeypatch) -> None:
    """Consecutive requests share the pool; only the connection is per request."""
    database = _FakeDatabase()
    _patch_database(monkeypatch, database)

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        return None

    app = _DatabaseBoundApp(inner, _UnboundContainer())

    async def three_requests() -> None:
        for _ in range(3):
            await app(_http_scope(), _receive, _send)

    asyncio.run(three_requests())
    assert database.opened == 1


def test_shutdown_closes_the_pool(monkeypatch) -> None:
    """Shutdown is tapped so connections are released, not dropped on exit."""
    database = _FakeDatabase()
    _patch_database(monkeypatch, database)
    sent: list[dict[str, Any]] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        # The SDK's own lifespan is what starts the session manager; the tap
        # must not swallow its messages.
        await send({"type": "lifespan.shutdown.complete"})

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    app = _DatabaseBoundApp(inner, _UnboundContainer())

    async def open_then_shut_down() -> None:
        await app(_http_scope(), _receive, _send)
        await app({"type": "lifespan"}, _receive, send)

    asyncio.run(open_then_shut_down())

    assert sent == [{"type": "lifespan.shutdown.complete"}]
    assert database.closed == 1
