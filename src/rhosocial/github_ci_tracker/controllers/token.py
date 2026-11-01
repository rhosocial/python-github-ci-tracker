# src/rhosocial/github_ci_tracker/controllers/token.py
"""The operator surface over credentials.

Adding, verifying, rotating, deactivating. Callers may name a credential by its
row id or by its alias, so the resolution lives here; the token itself is never
echoed back to the caller.

Every operation delegates to the provider that owns the credential. This module
normalizes arguments and shapes answers -- it is not a second implementation of
a credential. A second provider kind gets its own branch here rather than a
rewrite of the protocol adapters.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.exceptions import TokenNotFoundError
from ..credentials.redaction import redact_credential_logging
from ..services.github.protocols import CredentialProbe
from ..views import TokenRecord, as_utc

logger = logging.getLogger(__name__)

#: Provider kind this controller drives.
PROVIDER_KIND = "pat"


class TokenController:
    """Manage credentials: add, verify, list, rotate, delete.

    Args:
        probe: The GitHub probe used to verify a raw token and to measure a
            stored credential's capabilities.
    """

    def __init__(self, *, probe: CredentialProbe) -> None:
        self._probe = probe

    async def add_token(
        self,
        name: str,
        token: str,
        scope: str = "repo,workflow",
        probe_repo: str | None = None,
    ) -> TokenRecord:
        """Register a credential after probing its capabilities.

        Args:
            name: Operator-facing alias.
            token: The PAT value.
            scope: Informational scope.
            probe_repo: Repository used to test Actions write permission. The
                answer is repository-scoped, so without one ``can_write_actions``
                stays unknown and triggering remains refused.

        Raises:
            ValidationError: The token or its name is empty.
            TokenAlreadyExistsError: The name or the value is already stored.
            TokenInvalidError: GitHub rejected the token.
        """
        with redact_credential_logging():
            record = await self._provider().add(
                token=token,
                name=name,
                client=self._probe,
                scope=scope,
                verify=True,
                probe_repo=probe_repo,
            )
        return self._view(record)

    async def ensure_token(
        self,
        name: str,
        token: str,
        scope: str = "repo,workflow",
        probe_repo: str | None = None,
    ) -> tuple[TokenRecord, bool]:
        """Create the credential, or rotate it when the alias already exists.

        This is the idempotent entry point a first-boot init script needs: a
        fresh deployment creates the credential, a forced re-run reconciles the
        supplied token into the existing row. Rotation keeps the id and every
        repository binding, so re-provisioning does not lose subscriptions.

        Returns:
            ``(record, created)``: the stored credential, and whether it was
            created rather than rotated.
        """
        try:
            ref = await self._find_ref(name)
        except TokenNotFoundError:
            record = await self.add_token(
                name=name, token=token, scope=scope, probe_repo=probe_repo
            )
            return record, True
        return (
            await self._rotate(ref=ref, token=token, probe_repo=probe_repo),
            False,
        )

    async def list_tokens(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """List credentials without exposing the token values."""
        return await self._provider().list_metadata(limit=limit, offset=offset)

    async def verify_token(self, name: str) -> dict[str, Any]:
        """Probe what a stored credential can do.

        Returns:
            Capability dictionary including ``credential``.
        """
        ref = await self._find_ref(name)
        return await self._capabilities(ref, name, refresh=True)

    async def capabilities(self, name: str, probe_repo: str | None = None) -> dict[str, Any]:
        """Report a stored credential's capabilities.

        Args:
            name: Credential alias.
            probe_repo: Repository used to re-test Actions write permission. The
                answer is repository-scoped, so passing one is what turns an
                unknown capability into a measured one.
        """
        ref = await self._find_ref(name)
        return await self._capabilities(
            ref, name, refresh=probe_repo is not None, probe_repo=probe_repo
        )

    async def rotate_token(
        self,
        name: str,
        token: str,
        expires_at: Any = None,
        probe_repo: str | None = None,
    ) -> TokenRecord:
        """Replace a credential's value, keeping its id and bindings."""
        return await self._rotate(
            ref=await self._find_ref(name),
            token=token,
            expires_at=expires_at,
            probe_repo=probe_repo,
        )

    async def delete_token(self, name: str) -> None:
        """Permanently delete a stored credential.

        Raises:
            TokenInUseError: Repositories still bind to it.
        """
        await self._provider().delete(await self._find_ref(name))

    # -- internals -------------------------------------------------------------

    def _provider(self):
        """The provider that owns the credential rows."""
        from ..credentials import get_provider

        return get_provider(PROVIDER_KIND)

    async def _find_ref(self, name: str) -> int:
        """Resolve a credential alias to its provider id, or raise."""
        record = await self._provider().find_by_name(name)
        if record is None or record.id is None:
            raise TokenNotFoundError(f"Credential '{name}' not found")
        return record.id

    async def _rotate(
        self,
        *,
        ref: int,
        token: str,
        expires_at: Any = None,
        probe_repo: str | None = None,
    ) -> TokenRecord:
        """Rotate a credential, keeping its rows out of the query log."""
        with redact_credential_logging():
            record = await self._provider().rotate(
                ref=ref,
                new_token=token,
                client=self._probe,
                expires_at=expires_at,
                verify=True,
                probe_repo=probe_repo,
            )
        return self._view(record)

    @staticmethod
    def _view(record: Any) -> TokenRecord:
        """A credential row, reduced to what callers may see."""
        return TokenRecord(
            id=record.id or 0,
            name=record.name or "",
            scope=record.scope or "",
            owner_login=record.owner_login,
            is_active=bool(record.is_active),
            expires_at=as_utc(record.expires_at),
            can_read_actions=record.can_read_actions,
            can_write_actions=record.can_write_actions,
            can_manage_webhooks=record.can_manage_webhooks,
            created_at=as_utc(record.created_at),
            updated_at=as_utc(record.updated_at),
        )

    async def _capabilities(
        self,
        ref: int,
        name: str,
        *,
        refresh: bool = False,
        probe_repo: str | None = None,
    ) -> dict[str, Any]:
        """Measure a credential's capabilities and label the answer."""
        measured = await self._provider().capabilities(
            ref, refresh=refresh, client=self._probe, probe_repo=probe_repo
        )
        answer = measured.as_dict()
        answer["credential"] = f"{PROVIDER_KIND}/{name}"
        return answer


__all__ = ["TokenController"]
