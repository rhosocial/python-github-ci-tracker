# src/rhosocial/github_ci_tracker/adapters/rest/middleware/auth.py
"""API Key authentication middleware for REST API."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """API Key authentication middleware.

    Validates the Bearer token in the Authorization header against
    the CITRACKER_API_KEY environment variable.

    Webhook endpoints (under /webhooks/) bypass API key validation
    and use GitHub signature verification instead.
    Health/readiness endpoints are exempt.

    An empty ``CITRACKER_API_KEY`` disables REST authentication entirely: the
    local-development mode the CLI warns about at startup. Requiring a header
    that compares against "" would accept ``Authorization: Bearer `` and reject
    an absent header, which is neither authenticated nor open.

    Note: the middleware returns ``JSONResponse`` instead of raising
    ``HTTPException``. Under ``BaseHTTPMiddleware`` a raised exception escapes
    the exception-handler chain and surfaces as a 500, so the status code is
    produced directly.
    """

    #: Probes, unauthenticated by necessity: an orchestrator holds no API key,
    #: and a 401 on a probe path reads as "the service is down".
    HEALTH_PATHS = frozenset({"/health", "/healthz", "/livez", "/readyz"})

    def __init__(self, app: object, api_key: str | None = None) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.api_key = api_key or os.getenv("CITRACKER_API_KEY", "")

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Process the request through the middleware."""
        # Webhook endpoints use signature verification, skip API key check
        if request.url.path.startswith("/webhooks/"):
            return await call_next(request)

        # Health check endpoints require no authentication
        if request.url.path in self.HEALTH_PATHS:
            return await call_next(request)

        # Empty authorization is a supported local-development mode. The
        # protocol endpoint is then open, and the CLI logs a warning at start.
        # Comparing against an empty key would instead accept "Bearer " and
        # reject an absent header, which is neither authenticated nor open.
        if not self.api_key:
            return await call_next(request)

        # Validate API key
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "message": "Missing or invalid Authorization header",
                },
            )

        token = auth_header[7:]  # Remove "Bearer " prefix
        if token != self.api_key:
            return JSONResponse(
                status_code=403,
                content={"error": "forbidden", "message": "Invalid API key"},
            )

        return await call_next(request)
