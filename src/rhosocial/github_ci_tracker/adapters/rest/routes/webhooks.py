# src/rhosocial/github_ci_tracker/adapters/rest/routes/webhooks.py
"""Webhook routes."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os

from fastapi import APIRouter, HTTPException, Request
from starlette.requests import ClientDisconnect

router = APIRouter()

logger = logging.getLogger(__name__)


def get_webhook_service(request: Request):
    """Dependency: the process's webhook controller.

    The controller carries the app's database seam, so work that outlives the request --
    the eager log fetch of a failed job -- can take its own connection instead
    of borrowing the one the response is about to return.
    """
    return request.app.state.container.webhook


def verify_github_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook signature.

    Args:
        payload: Raw request body.
        signature: X-Hub-Signature-256 header value.
        secret: Webhook secret.

    Returns:
        True if signature is valid.
    """
    if not signature.startswith("sha256="):
        return False
    expected_mac = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    provided_mac = signature[7:]  # Remove "sha256=" prefix
    return hmac.compare_digest(expected_mac, provided_mac)


@router.post("/webhooks/github")
async def github_webhook(request: Request) -> dict:
    """Receive GitHub webhook events.

    This endpoint bypasses API key authentication and uses
    GitHub's HMAC signature verification instead.

    A rejected delivery is recorded before the error is raised: "GitHub says it
    delivered but nothing arrived" and "it arrived with a bad signature" are
    different failures, and only the ledger tells them apart.

    A client that hangs up mid-body is not an error to report: GitHub does that
    when its own send times out, and the next attempt (or the next ``find_runs``)
    is what repairs the gap. Without this the disconnect surfaces as an
    unhandled ASGI exception and a traceback per aborted delivery, which crowds
    the log with something nobody can act on.
    """
    try:
        payload = await request.body()
    except ClientDisconnect:
        logger.info(
            "Webhook delivery %s: client disconnected before the body arrived",
            request.headers.get("X-GitHub-Delivery", ""),
        )
        return {"status": "disconnected"}

    signature = request.headers.get("X-Hub-Signature-256", "")
    event_type = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    secret = os.getenv("CITRACKER_WEBHOOK_SECRET", "")

    service = get_webhook_service(request)

    if secret and not verify_github_signature(payload, signature, secret):
        await service.record_signature_failure(event_type or "unknown", delivery_id)
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    result = await service.process_webhook(
        event_type=event_type,
        payload=payload,
        delivery_id=delivery_id,
    )
    return {"status": "processed", "event": event_type, **result}
