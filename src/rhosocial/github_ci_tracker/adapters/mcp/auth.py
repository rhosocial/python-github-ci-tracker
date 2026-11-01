# src/rhosocial/github_ci_tracker/adapters/mcp/auth.py
"""Static bearer authentication for the MCP adapter.

The service has no authorization server yet, so the operator key that REST
already enforces is the only credential.  This module adapts that key to the two
interfaces the MCP path needs:

* ``CredentialSource.verify`` for ``rhosocial.mcpserver``'s ASGI layers
  (scope enforcement, principal resolution);
* the MCP SDK's ``TokenVerifier.verify_token`` for its own ``BearerAuthBackend``.

Keeping both in one class is deliberate: a second implementation is the copy
that eventually compares differently, and a differing comparison here is a full
authentication bypass rather than a bug someone notices.

``metadata()`` returns ``None`` because there is no authorization server to
advertise.  The RFC 9728 document is therefore a 404, which is the honest
answer: a client that follows it would look for an issuer that does not exist.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterable

from mcp.server.auth.provider import AccessToken
from rhosocial.mcpserver import SELF_ISSUED, Principal, resolve_for_sdk

__all__ = ["STATIC_SUBJECT", "StaticBearerCredentialSource"]

#: The identity a static key acts as.  Not a user id: the key belongs to the
#: operator, and every tool call is attributed to that one principal.
STATIC_SUBJECT = "operator"


class StaticBearerCredentialSource:
    """Resolve ``Authorization: Bearer <CITRACKER_API_KEY>`` to a principal.

    :param api_key: the configured key.  Empty is refused: a source that
        accepts an empty string would turn "no credential" into "the operator".
    :param scopes: the scopes granted to the key.  There is one caller, so this
        is the service's whole vocabulary rather than a per-user grant.
    """

    __slots__ = ("_key", "_scopes")

    def __init__(self, api_key: str, scopes: Iterable[str]) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._key = api_key
        self._scopes = frozenset(scopes)

    def metadata(self) -> None:
        """No authorization server to advertise; the PRM endpoint answers 404."""
        return None

    async def verify(self, token: str) -> Principal | None:
        """Return the operator principal, or ``None`` for any other string.

        The comparison is constant-time and done on bytes: ``compare_digest``
        refuses non-ASCII ``str`` operands, and a caller-controlled token must
        not be able to turn a refusal into a 500.
        """
        if not token:
            return None
        if not hmac.compare_digest(token.encode("utf-8"), self._key.encode("utf-8")):
            return None
        return Principal(
            central_user_id=STATIC_SUBJECT,
            client_id=SELF_ISSUED,
            scopes=self._scopes,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        """The SDK's view of :meth:`verify`; ``None`` means "answer 401"."""
        return await resolve_for_sdk(self, token)
