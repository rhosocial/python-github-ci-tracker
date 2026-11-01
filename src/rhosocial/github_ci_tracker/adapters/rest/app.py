# src/rhosocial/github_ci_tracker/adapters/rest/app.py
"""FastAPI application factory for CI Tracker REST API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ... import __version__
from ...build import build_info
from ...config import get_settings
from ...container import Container
from ...core.exceptions import CitrackerError as CoreCitrackerError
from ...database import Database
from ..common.errors import CitrackerError, from_core_exception
from .middleware.auth import ApiKeyMiddleware
from .routes import jobs, repositories, runs, tokens, webhooks, workflows


def _error_response(exc: CitrackerError) -> JSONResponse:
    """Render one adapter error in the wire envelope shared by both handlers."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.code.value,
            "message": exc.message,
            "details": exc.details,
        },
    )


def create_app(
    container: Container | None = None,
    *,
    database: Database | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        container: The controllers this application serves from. ``serve all``
            runs two uvicorn servers -- REST and the webhook receiver -- and
            uvicorn runs the lifespan once per server; handing both the same
            container keeps one pool and one set of controllers, and keeps a
            second startup from replacing what the first server is serving.
        database: The process's database seam, when the caller owns it but has
            not built a container yet (``serve all`` does exactly that). Ignored
            when ``container`` is given.

    Returns:
        Configured FastAPI application instance.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Open the database scope this application serves from.

        A container handed in is already wired to its database and is used as
        is; otherwise the pool is created here and closed at shutdown. Every
        request coroutine acquires its own connection, so concurrent requests
        never share one. The scope is opened per request by the middleware
        below, not here.
        """
        ready = container
        owned: Database | None = None
        if ready is None:
            owned = database if database is not None else await Database.create(get_settings())
            # The controllers are built once, inside a bound connection:
            # resolving the ambient PAT is a query like any other.
            async with owned.connection():
                ready = await Container.start(owned)
        app.state.container = ready
        try:
            yield
        finally:
            if owned is not None:
                await owned.close()

    app = FastAPI(
        title="CI Tracker API",
        description="GitHub CI status tracking and automation service",
        version=__version__,
        lifespan=lifespan,
    )

    # A container handed in is available from the moment the app exists, so a
    # caller that swaps the lifespan (a test driving an in-memory database) does
    # not have to publish it a second time.
    if container is not None:
        app.state.container = container

    # Register middleware
    app.add_middleware(ApiKeyMiddleware)

    # Register routes
    app.include_router(workflows.router, prefix="/api/v1")
    app.include_router(runs.router, prefix="/api/v1")
    app.include_router(jobs.router, prefix="/api/v1")
    app.include_router(repositories.router, prefix="/api/v1")
    app.include_router(tokens.router, prefix="/api/v1")
    app.include_router(webhooks.router)

    # Per-request database scope: one pooled connection for the duration of the
    # request, so a request and a tool call resolve the models identically.
    @app.middleware("http")
    async def scope_database(request: Request, call_next):
        """Give this request its own connection.

        Probes are exempt: ``/livez`` must do no I/O at all (a liveness probe
        that acquires a connection turns a saturated pool into a restart loop),
        and ``/readyz`` asks the database itself, with its own timeout, so
        acquiring a connection here would only widen what it is measuring.
        """
        if request.url.path in ApiKeyMiddleware.HEALTH_PATHS:
            return await call_next(request)
        current = getattr(app.state, "container", None)
        if current is None or current.database is None:
            return await call_next(request)
        async with current.database.connection():
            return await call_next(request)

    # Probes. Two questions, deliberately answered by two paths:
    #
    # * ``/livez`` -- is this process alive? No I/O at all. Failing it makes an
    #   orchestrator restart the process, so it must only fail when a restart
    #   can help; a database outage cannot be restarted away, and a liveness
    #   probe that touched the database would turn one outage into a restart
    #   loop that reconnects and amplifies it.
    # * ``/readyz`` -- can this instance serve a request right now? It asks the
    #   database, because every flow reads or writes a row. Failing it removes
    #   the instance from rotation without restarting it.
    #
    # ``/health`` and ``/healthz`` are readiness under the names the compose
    # files and existing monitors already use; their comments promised a
    # database round trip, and this is where that promise is kept.
    #
    # Both probes carry the build stamp (commit, build number, build time). It
    # is the one answer that makes "is the deployment the commit I pushed?"
    # checkable without reading a tool description: ``version`` alone cannot
    # distinguish two images of the same source tree.
    @app.get("/livez")
    async def livez() -> dict:
        """Liveness: the process is running and the event loop answers."""
        return {
            "status": "alive",
            "version": __version__,
            "build": build_info().as_dict(),
        }

    @app.get("/readyz")
    @app.get("/health")
    @app.get("/healthz")
    async def readyz() -> JSONResponse:
        """Readiness: this instance can serve -- today, that means the database."""
        current = getattr(app.state, "container", None)
        if current is None:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "build": build_info().as_dict(),
                    "checks": {"container": "not built"},
                },
            )
        report = await current.readiness()
        ready = bool(report["ready"])
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "not_ready",
                "version": __version__,
                "build": build_info().as_dict(),
                "checks": report["checks"],
            },
        )

    # Global exception handler for adapter errors raised by the routes.
    @app.exception_handler(CitrackerError)
    async def citracker_exception_handler(
        request: Request,
        exc: CitrackerError,
    ) -> JSONResponse:
        """Handle CitrackerError exceptions."""
        return _error_response(exc)

    # The business core raises its own hierarchy, a different class from the
    # adapter's, so it needs its own translation: without this handler an
    # unsubscribed repository or an unknown workflow id reaches the client as
    # an opaque 500 instead of the 404 it deserves.
    @app.exception_handler(CoreCitrackerError)
    async def core_exception_handler(
        request: Request,
        exc: CoreCitrackerError,
    ) -> JSONResponse:
        """Translate a core exception and answer in the same envelope."""
        return _error_response(from_core_exception(exc))

    return app
