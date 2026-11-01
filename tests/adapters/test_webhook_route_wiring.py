# tests/adapters/test_webhook_route_wiring.py
"""The webhook route must take its controller from the application's container.

``get_webhook_service`` needs the request to reach ``app.state.container`` (the
webhook controller carries the database seam, so the deferred log fetch can take
a connection of its own). The signature changed once and the call site did not,
so every delivery answered 500 with ``missing 1 required positional argument`` --
a failure the unit suite could not see, because the integration tests that drive
the real route are skipped without PostgreSQL.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from rhosocial.github_ci_tracker.adapters.rest.routes.webhooks import github_webhook


def _request(body: bytes, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    sent = {"done": False}

    async def receive() -> dict:
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/webhooks/github",
        "raw_path": b"/webhooks/github",
        "query_string": b"",
        "headers": headers or [],
        "client": ("127.0.0.1", 1234),
        "server": ("citracker.test", 443),
        "app": _App(),
    }
    return Request(scope, receive=receive)


class _Webhook:
    """Refuses to be used: a forged delivery must not reach the database."""

    def __init__(self) -> None:
        self.recorded: list[tuple[str, str]] = []

    async def record_signature_failure(self, event_type: str, delivery_id: str) -> None:
        self.recorded.append((event_type, delivery_id))

    async def process_webhook(self, **_: object) -> dict:
        raise AssertionError("a forged delivery reached the delivery handler")


class _State:
    container = SimpleNamespace(webhook=_Webhook())


class _App:
    state = _State()


def test_the_route_reaches_its_own_signature_check(monkeypatch) -> None:
    """A forged delivery is refused before anything reaches the database."""
    monkeypatch.setenv("CITRACKER_WEBHOOK_SECRET", "s3cret")
    request = _request(
        json.dumps({"action": "completed"}).encode(),
        [
            (b"x-github-event", b"workflow_run"),
            (b"x-github-delivery", b"d-forged"),
            (b"x-hub-signature-256", b"sha256=deadbeef"),
            (b"content-type", b"application/json"),
        ],
    )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(github_webhook(request))

    assert (
        caught.value.status_code == 401
    ), "an unexpected error here means the route cannot build its controller"
    assert _State.container.webhook.recorded == [("workflow_run", "d-forged")]


def _disconnected_request() -> Request:
    """A request whose client hangs up before the body arrives."""

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/webhooks/github",
        "raw_path": b"/webhooks/github",
        "query_string": b"",
        "headers": [(b"x-github-delivery", b"d-aborted")],
        "client": ("127.0.0.1", 1234),
        "server": ("citracker.test", 443),
        "app": _App(),
    }
    return Request(scope, receive=receive)


def test_a_client_that_hangs_up_mid_body_is_not_a_server_error(monkeypatch) -> None:
    """GitHub abandons a send now and then; that is not a 500 to report.

    Left uncaught it surfaced as ``ClientDisconnect`` inside ``Exception in ASGI
    application`` -- one traceback per aborted delivery, all of it noise the
    operator cannot act on.
    """
    monkeypatch.setenv("CITRACKER_WEBHOOK_SECRET", "s3cret")

    result = asyncio.run(github_webhook(_disconnected_request()))

    assert result == {"status": "disconnected"}
