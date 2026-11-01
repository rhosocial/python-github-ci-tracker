# tests/adapters/test_probes.py
"""Liveness and readiness answer different questions, and only one asks the DB.

A liveness probe that touches the database turns one outage into a restart
loop: the process is fine, the database is not, and restarting reconnects and
amplifies the failure. A readiness probe that does not touch it keeps a broken
instance in the load balancer, answering 500 to every request.

So ``/livez`` does no I/O at all, and ``/readyz`` asks the database for one
round trip. The older names (``/health``, ``/healthz``) are readiness, because
that is what the deployment's health checks were written against.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rhosocial.github_ci_tracker.adapters.mcp.server import (
    LIVEZ_PATH,
    READYZ_PATHS,
    _DatabaseBoundApp,
    _UnboundContainer,
)
from rhosocial.github_ci_tracker.adapters.rest.app import create_app
from rhosocial.github_ci_tracker.build import NUMBER_ENV, REVISION_ENV
from rhosocial.github_ci_tracker.container import Container
from rhosocial.github_ci_tracker.database import Database

API_KEY = "probe-key"


class _Database:
    """A database seam whose answer the test decides."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.pings = 0
        self.connections = 0

    async def ping(self, *, timeout: float = 2.0) -> None:
        self.pings += 1
        if self.fail is not None:
            raise self.fail

    def connection(self) -> Any:
        raise AssertionError("a liveness probe must not open a connection")


def _container(database: Any, *, schema: str | None = None) -> SimpleNamespace:
    """The slice of a container the routes touch."""

    async def readiness() -> dict[str, Any]:
        checks: dict[str, str] = {"container": "ok"}
        ready = True
        if database is None:
            ready = False
            checks["database"] = "not configured"
        else:
            try:
                await database.ping()
            except Exception as exc:  # noqa: BLE001
                ready = False
                checks["database"] = f"{type(exc).__name__}: {exc}"
            else:
                checks["database"] = "ok"
        if schema is not None:
            checks["schema"] = schema
        return {"ready": ready, "checks": checks}

    return SimpleNamespace(database=database, readiness=readiness)


def _app(container: Any) -> Any:
    app = create_app(container)  # type: ignore[arg-type]
    # The production lifespan would open a pool; the container is already built.
    return app


class TestRestProbes:
    def test_liveness_never_touches_the_database(self, monkeypatch) -> None:
        monkeypatch.setenv("CITRACKER_API_KEY", API_KEY)
        app = _app(_container(_Database(fail=TimeoutError("unreachable"))))

        with TestClient(app) as client:
            response = client.get("/livez")

        assert response.status_code == 200
        assert response.json()["status"] == "alive"

    def test_readiness_reports_an_unreachable_database(self, monkeypatch) -> None:
        monkeypatch.setenv("CITRACKER_API_KEY", API_KEY)
        database = _Database(fail=TimeoutError("no answer within 2s"))
        app = _app(_container(database))

        with TestClient(app) as client:
            response = client.get("/readyz")

        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
        assert "no answer within 2s" in response.json()["checks"]["database"]
        assert database.pings == 1

    def test_readiness_is_ok_when_the_database_answers(self, monkeypatch) -> None:
        monkeypatch.setenv("CITRACKER_API_KEY", API_KEY)
        app = _app(_container(_Database(), schema="ok"))

        with TestClient(app) as client:
            response = client.get("/readyz")

        assert response.status_code == 200
        assert response.json()["checks"] == {"container": "ok", "database": "ok", "schema": "ok"}

    def test_readiness_without_a_database_is_not_ready(self, monkeypatch) -> None:
        monkeypatch.setenv("CITRACKER_API_KEY", API_KEY)
        app = _app(_container(None))

        with TestClient(app) as client:
            response = client.get("/readyz")

        assert response.status_code == 503
        assert response.json()["checks"]["database"] == "not configured"

    @pytest.mark.parametrize("path", ["/health", "/healthz", "/readyz"])
    def test_the_probe_paths_need_no_api_key(self, monkeypatch, path: str) -> None:
        monkeypatch.setenv("CITRACKER_API_KEY", API_KEY)
        app = _app(_container(_Database()))

        with TestClient(app) as client:
            response = client.get(path)

        assert response.status_code == 200, "an orchestrator holds no API key"

    def test_a_missing_table_is_reported_without_failing_the_probe(self, monkeypatch) -> None:
        """``init`` creates the schema; draining the instance would not."""
        monkeypatch.setenv("CITRACKER_API_KEY", API_KEY)
        app = _app(_container(_Database(), schema="missing: ct_jobs"))

        with TestClient(app) as client:
            response = client.get("/readyz")

        assert response.status_code == 200
        assert response.json()["checks"]["schema"] == "missing: ct_jobs"

    def test_both_probes_carry_the_build_stamp(self, monkeypatch) -> None:
        """``version`` cannot tell two images apart; the stamp can."""
        monkeypatch.setenv("CITRACKER_API_KEY", API_KEY)
        monkeypatch.setenv(REVISION_ENV, "e5815a5f5b31df495dc69638d11602df685a0e4b")
        monkeypatch.setenv(NUMBER_ENV, "42")
        app = _app(_container(_Database()))

        with TestClient(app) as client:
            live = client.get("/livez").json()
            ready = client.get("/readyz").json()

        assert live["build"]["short_revision"] == "e5815a5"
        assert live["build"]["number"] == "42"
        assert ready["build"]["revision"] == "e5815a5f5b31df495dc69638d11602df685a0e4b"

    def test_an_unstamped_build_is_reported_as_null(self, monkeypatch) -> None:
        monkeypatch.setenv("CITRACKER_API_KEY", API_KEY)
        monkeypatch.delenv(REVISION_ENV, raising=False)
        monkeypatch.delenv(NUMBER_ENV, raising=False)
        app = _app(_container(_Database()))

        with TestClient(app) as client:
            build = client.get("/readyz").json()["build"]

        assert build["revision"] is None and build["number"] is None


def _probe_scope(path: str, method: str = "GET") -> dict[str, Any]:
    return {"type": "http", "method": method, "path": path, "headers": []}


async def _receive() -> dict[str, Any]:
    return {"type": "http.request"}


class _Sent:
    """Capture what an ASGI app wrote."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.body = b""

    async def __call__(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            self.body = message.get("body", b"")


class TestMcpProbes:
    def test_readiness_is_answered_before_the_sdk_sees_the_request(self) -> None:
        called: list[str] = []

        async def inner(scope: Any, receive: Any, send: Any) -> None:
            called.append(scope["path"])

        services = _UnboundContainer()
        services.bind(_container(_Database()))
        app = _DatabaseBoundApp(inner, services)

        sent = _Sent()
        asyncio.run(app(_probe_scope(READYZ_PATHS[0]), _receive, sent))

        assert sent.status == 200
        assert called == [], "the SDK must not see a probe"

    def test_liveness_answers_even_when_the_database_is_down(self) -> None:
        async def inner(scope: Any, receive: Any, send: Any) -> None:
            raise AssertionError("liveness must not reach the MCP app")

        services = _UnboundContainer()
        services.bind(_container(_Database(fail=RuntimeError("down"))))
        app = _DatabaseBoundApp(inner, services)

        sent = _Sent()
        asyncio.run(app(_probe_scope(LIVEZ_PATH), _receive, sent))

        assert sent.status == 200
        assert b"alive" in sent.body

    def test_the_mcp_probes_report_the_build_too(self, monkeypatch) -> None:
        """An MCP-only deployment's probe must answer the same question."""
        monkeypatch.setenv(REVISION_ENV, "e5815a5f5b31df495dc69638d11602df685a0e4b")

        async def inner(scope: Any, receive: Any, send: Any) -> None:
            raise AssertionError("a probe must not reach the MCP app")

        services = _UnboundContainer()
        services.bind(_container(_Database()))
        app = _DatabaseBoundApp(inner, services)

        sent = _Sent()
        asyncio.run(app(_probe_scope(READYZ_PATHS[0]), _receive, sent))

        assert sent.status == 200
        assert json.loads(sent.body)["build"]["short_revision"] == "e5815a5"


class _SlowBackend:
    async def execute(self, sql: str) -> None:
        await asyncio.sleep(1.0)


class _SlowPool:
    """A pool whose connection never finishes answering in time."""

    @asynccontextmanager
    async def connection(self):
        yield _SlowBackend()


class TestDatabasePing:
    async def test_a_slow_database_times_out_instead_of_hanging(self) -> None:
        """The probe must answer; "still waiting" reads as a dead process."""
        database = Database(_SlowPool())  # type: ignore[arg-type]

        with pytest.raises(TimeoutError, match="no answer within"):
            await database.ping(timeout=0.01)


class TestContainerReadiness:
    async def test_without_a_database_the_answer_is_not_ready(self) -> None:
        report = await Container.build().readiness()

        assert report["ready"] is False
        assert report["checks"]["database"] == "not configured"

    async def test_a_reachable_database_is_ready(self) -> None:
        database = _Database()
        container = Container.build(database=database)  # type: ignore[arg-type]

        report = await container.readiness()

        assert report["ready"] is True
        assert report["checks"]["database"] == "ok"
        assert await container.healthy() is True

    async def test_an_unreachable_database_is_reported_by_name(self) -> None:
        container = Container.build(database=_Database(fail=RuntimeError("refused")))  # type: ignore[arg-type]

        report = await container.readiness()

        assert report["ready"] is False
        assert report["checks"]["database"] == "RuntimeError: refused"
        assert await container.healthy() is False
