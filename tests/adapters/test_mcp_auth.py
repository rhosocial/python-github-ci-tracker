# tests/adapters/test_mcp_auth.py
"""Static bearer wiring for the MCP adapter.

The key is the only credential until an authorization server exists, so these
tests pin three things: the comparison accepts exactly one string, the SDK sees
the scopes the ASGI layers check, and the app chooses the authenticated stack
only when a key is configured.
"""

from __future__ import annotations

import asyncio

from rhosocial.mcpserver import SELF_ISSUED, ResourceEndpoints

from rhosocial.github_ci_tracker.adapters.mcp.auth import (
    STATIC_SUBJECT,
    StaticBearerCredentialSource,
)
from rhosocial.github_ci_tracker.adapters.mcp.scopes import SCOPES
from rhosocial.github_ci_tracker.adapters.mcp.server import create_mcp_app

KEY = "sm_test_key"


def _layers(app: object) -> list[object]:
    """Every wrapper between the ASGI entry point and the SDK app.

    ``create_mcp_app`` returns the database-bound wrapper, so the auth stack is
    no longer the outermost object; walk ``_app`` the way the wrappers chain.
    """
    layers: list[object] = []
    current = app
    while current is not None and current not in layers:
        layers.append(current)
        current = getattr(current, "_app", None)
    return layers


def test_accepts_only_the_configured_key() -> None:
    """One exact string is the operator; everything else is anonymous."""
    source = StaticBearerCredentialSource(KEY, SCOPES)

    principal = asyncio.run(source.verify(KEY))
    assert principal is not None
    assert principal.central_user_id == STATIC_SUBJECT
    assert principal.client_id == SELF_ISSUED
    assert principal.scopes == frozenset(SCOPES)

    assert asyncio.run(source.verify("sm_other")) is None
    assert asyncio.run(source.verify("")) is None
    # A non-ASCII token must be refused rather than raising: compare_digest on
    # str operands rejects non-ASCII instead of returning False.
    assert asyncio.run(source.verify("密钥")) is None
    assert source.metadata() is None


def test_sdk_token_carries_the_scopes() -> None:
    """The SDK's AccessToken is what its BearerAuthBackend checks."""
    source = StaticBearerCredentialSource(KEY, SCOPES)

    token = asyncio.run(source.verify_token(KEY))
    assert token is not None
    assert token.subject == STATIC_SUBJECT
    assert token.client_id == SELF_ISSUED
    assert token.scopes == sorted(SCOPES)
    assert asyncio.run(source.verify_token("sm_other")) is None


def test_api_key_selects_the_authenticated_stack(monkeypatch) -> None:
    """A configured key must produce the ASGI layers, not the bare SDK app."""
    monkeypatch.setenv("CITRACKER_API_KEY", KEY)
    monkeypatch.delenv("CITRACKER_OAUTH_ISSUER", raising=False)

    assert any(isinstance(layer, ResourceEndpoints) for layer in _layers(create_mcp_app()))


def test_without_credentials_the_development_fallback_stays(monkeypatch) -> None:
    """No key and no issuer is the unauthenticated local-only mode."""
    monkeypatch.delenv("CITRACKER_API_KEY", raising=False)
    monkeypatch.delenv("CITRACKER_OAUTH_ISSUER", raising=False)

    assert not any(isinstance(layer, ResourceEndpoints) for layer in _layers(create_mcp_app()))
