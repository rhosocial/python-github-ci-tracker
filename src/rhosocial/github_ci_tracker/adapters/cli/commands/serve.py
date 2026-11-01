# src/rhosocial/github_ci_tracker/adapters/cli/commands/serve.py
"""rhosocial-github-ci-tracker serve command."""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from ..eventloop import run_coroutine

log = logging.getLogger(__name__)


def run(args: argparse.Namespace, api_key: str) -> None:
    """Execute the serve command.

    Args:
        args: Parsed command-line arguments
        api_key: API key for authentication
    """
    from ....config import get_settings

    settings = get_settings().service
    _resolve_bind_defaults(args, settings)

    if args.service == "rest":
        _serve_rest(args, settings)
    elif args.service == "mcp":
        _serve_mcp(args)
    elif args.service == "webhook":
        _serve_webhook(args, settings)
    elif args.service == "all":
        _serve_all(args, settings)


def _resolve_bind_defaults(args: argparse.Namespace, settings: Any) -> None:
    """Fill unspecified --host/--port from the configured values."""
    defaults = {
        "rest": (settings.rest_host, settings.rest_port),
        "mcp": (settings.mcp_host, settings.mcp_port),
        "webhook": (settings.webhook_host, settings.webhook_port),
        "all": (settings.rest_host, settings.rest_port),
    }
    host, port = defaults.get(args.service, (settings.rest_host, settings.rest_port))
    if args.host is None:
        args.host = host
    if args.port is None:
        args.port = port


def _serve(
    coro_factory,
    host: str,
    port: int,
    *,
    ssl_certfile: str = "",
    ssl_keyfile: str = "",
) -> None:
    """Run a uvicorn server on a loop psycopg accepts on Windows.

    psycopg cannot drive sockets on the ProactorEventLoop, so on Windows the
    server runs on a SelectorEventLoop. ``set_event_loop_policy`` is deprecated
    in 3.14+, so the loop is created through ``asyncio.Runner`` and uvicorn is
    driven with its ``Server.serve()`` API instead of the blocking
    ``uvicorn.run()`` (which installs its own loop).

    ``ssl_certfile`` / ``ssl_keyfile`` are a full-chain PEM and its key; both
    empty (the default) serves plain HTTP, which is what the local-only REST
    and MCP listeners use. The webhook listener needs them because GitHub
    refuses an http callback URL.
    """
    import uvicorn

    config = uvicorn.Config(
        app=coro_factory(),
        host=host,
        port=port,
        log_level="info",
        ssl_certfile=ssl_certfile or None,
        ssl_keyfile=ssl_keyfile or None,
    )
    server = uvicorn.Server(config)

    run_coroutine(server.serve())


def _serve_rest(args: argparse.Namespace, settings: Any) -> None:
    """Start REST API server."""
    from ...rest.app import create_app

    _warn_if_unauthenticated(settings)
    _serve(create_app, args.host, args.port)


def _warn_if_unauthenticated(settings: Any) -> None:
    """Announce that the REST surface is open.

    Empty authorization is a supported local-development mode; the warning is
    what keeps it from being discovered in production by a stranger.
    """
    if not settings.api_key:
        log.warning(
            "REST API authentication is disabled: CITRACKER_API_KEY is empty, "
            "so any caller can use /api/v1. Set a key before exposing this "
            "port publicly."
        )


def _serve_mcp(args: argparse.Namespace) -> None:
    """Start MCP server."""
    from ...mcp.server import create_mcp_app

    _serve(create_mcp_app, args.host, args.port)


def _serve_webhook(args: argparse.Namespace, settings: Any) -> None:
    """Start webhook receiver server (same app as REST, different port).

    The receiver terminates TLS itself: GitHub delivers only to an https URL
    with a publicly trusted certificate, so the configured full-chain PEM and
    its key are handed to uvicorn here. Without them the listener serves plain
    HTTP and GitHub refuses to register the callback.
    """
    from ...rest.app import create_app

    _serve(
        create_app,
        args.host,
        args.port,
        ssl_certfile=settings.webhook_ssl_certfile,
        ssl_keyfile=settings.webhook_ssl_keyfile,
    )


def _serve_all(args: argparse.Namespace, settings: Any) -> None:
    """Start the REST API and webhook receiver on separate ports.

    REST stays plain HTTP on purpose -- it is a local-only surface. The webhook
    receiver carries the TLS material GitHub demands for its callback URL.
    """
    import uvicorn

    from ...rest.app import create_app

    _warn_if_unauthenticated(settings)

    async def serve_both() -> None:
        from ....container import Container
        from ....database import Database

        # One database, one container, two servers. uvicorn runs the lifespan
        # once per server, so handing both the same container is what keeps one
        # pool and one set of controllers: a second startup would otherwise build
        # its own while the first server is serving.
        database = await Database.create()
        try:
            async with database.connection():
                container = await Container.start(database)
            rest_config = uvicorn.Config(
                create_app(container),
                host=settings.rest_host,
                port=settings.rest_port,
                log_level="info",
            )
            webhook_config = uvicorn.Config(
                create_app(container),
                host=settings.webhook_host,
                port=settings.webhook_port,
                log_level="info",
                ssl_certfile=settings.webhook_ssl_certfile or None,
                ssl_keyfile=settings.webhook_ssl_keyfile or None,
            )
            await asyncio.gather(
                uvicorn.Server(rest_config).serve(),
                uvicorn.Server(webhook_config).serve(),
            )
        finally:
            await database.close()

    run_coroutine(serve_both())
