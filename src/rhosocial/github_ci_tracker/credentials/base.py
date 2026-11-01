# src/rhosocial/github_ci_tracker/credentials/base.py
"""Credential providers: where a GitHub token comes from.

The core services need one thing -- a token string to put in an Authorization
header -- but the *source* of that token varies. A fine-grained PAT is a value
the operator typed; a GitHub App installation token is minted on demand from a
private key and expires in an hour. Those differ in lifetime, refresh strategy,
identity and storage, so they live in separate tables behind separate providers
rather than being forced into one credential table with nullable columns.

The core knows only an opaque pair: ``(provider_kind, provider_ref)``. It never
imports a provider's models, and it never inspects a provider's tables. Adding a
provider is therefore additive:

    table + model + provider class + CLI module  ->  registry entry

with no change to the business core.

``resolve`` is async because an App provider has to call GitHub to mint a token,
while the PAT provider only reads the database. Keeping the difference inside
``resolve`` means callers always write the same line.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Capabilities:
    """What a credential is allowed to do, as measured rather than declared.

    A fine-grained PAT carries whatever permissions the user granted it; there is
    no configuration switch to consult. Probing is what makes ``can_write``
    trustworthy enough to gate a trigger on.
    """

    is_valid: bool = False
    #: Display identity: the PAT owner's login, or an App installation account.
    identity: str = ""
    can_read_actions: bool = False
    can_write_actions: bool = False
    can_manage_webhooks: bool = False
    accessible_repos: int = 0
    probed_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialize for protocol responses."""
        return {
            "is_valid": self.is_valid,
            "identity": self.identity,
            "can_read_actions": self.can_read_actions,
            "can_write_actions": self.can_write_actions,
            "can_manage_webhooks": self.can_manage_webhooks,
            "accessible_repos": self.accessible_repos,
            "probed_at": self.probed_at.isoformat() if self.probed_at else None,
        }


@dataclass(frozen=True)
class ResolvedCredential:
    """A token ready to use, plus what it may do.

    ``token`` is a secret: it goes into a request header and is never logged,
    serialized into a response, or written to a log line.

    ``kind`` and ``ref`` are the provider pair the binding table stores. They
    are carried here so a component that holds a credential -- the ambient PAT
    of a process, most of all -- can *write* a binding without going back to
    the database to ask which credential it is holding. Both stay empty for a
    credential that was not materialized from a provider row, and nothing may
    bind one in that case.
    """

    token: str
    identity: str = ""
    expires_at: datetime | None = None
    can_read: bool = True
    can_write: bool = False
    can_manage_webhooks: bool = False
    #: Provider registry key, e.g. ``"pat"``. Empty when the credential was not
    #: resolved from a provider row.
    kind: str = ""
    #: Provider-owned id (``ct_pat_credentials.id``). ``None`` for the same case.
    ref: int | None = None

    def __repr__(self) -> str:
        """Never print the secret."""
        return (
            f"ResolvedCredential(identity={self.identity!r}, "
            f"expires_at={self.expires_at!r}, can_write={self.can_write!r})"
        )


@runtime_checkable
class IAsyncCredentialProvider(Protocol):
    """Async credential source (the twins the service uses)."""

    #: Registry key, e.g. ``"pat"``. Stable: it is persisted in binding rows.
    kind: str

    async def resolve(self, ref: int) -> ResolvedCredential:
        """Materialize a usable token for ``ref``.

        Raises:
            TokenNotFoundError: The referenced credential does not exist.
            TokenDeactivatedError: It exists but was deactivated.
            TokenExpiredError: It is past its declared expiry.
        """
        ...

    async def capabilities(self, ref: int, refresh: bool = False) -> Capabilities:
        """Report what ``ref`` can do, probing GitHub when useful."""
        ...

    async def describe(self, ref: int) -> dict[str, Any]:
        """Describe ``ref`` for status output, never exposing the secret."""
        ...

    async def count_bindings(self, ref: int) -> int:
        """How many repositories are bound to ``ref``."""
        ...

    async def release(self, ref: int) -> int:
        """Drop the bindings of ``ref``; returns how many were removed."""
        ...


@dataclass
class _Registry:
    """Kind -> provider pair: the async twin the service uses, and its sync twin.

    Both twins are registered under the same ``kind`` because the binding row
    stores only the kind. A sync caller (the sync model twin, kept for parity)
    needs the sync implementation: running the async one through an event loop
    inside a sync method is exactly the kind of hidden blocking the twin split
    exists to avoid.

    The sync twin is optional. A provider that can only mint tokens over HTTP
    asynchronously registers nothing here, and a sync caller gets a clear error
    instead of a deadlock.
    """

    async_providers: dict[str, IAsyncCredentialProvider] = field(default_factory=dict)
    sync_providers: dict[str, Any] = field(default_factory=dict)

    def register(
        self,
        provider: IAsyncCredentialProvider,
        sync_twin: Any | None = None,
    ) -> None:
        """Register ``provider`` (and optionally its sync twin) under its kind."""
        if not provider.kind:
            raise ValueError("a credential provider must declare a kind")
        self.async_providers[provider.kind] = provider
        if sync_twin is not None:
            self.sync_providers[provider.kind] = sync_twin

    def get(self, kind: str) -> IAsyncCredentialProvider:
        """Return the async provider for ``kind``.

        Raises:
            KeyError: If no provider is registered; the message lists what is,
                because the usual cause is a binding row from a provider this
                build does not have.
        """
        try:
            return self.async_providers[kind]
        except KeyError:
            known = ", ".join(sorted(self.async_providers)) or "none"
            raise KeyError(
                f"no credential provider registered for kind '{kind}' (known: {known})"
            ) from None

    def get_sync(self, kind: str) -> Any:
        """Return the sync provider for ``kind``.

        Raises:
            KeyError: If the kind is unknown or has no sync twin.
        """
        try:
            return self.sync_providers[kind]
        except KeyError:
            known = ", ".join(sorted(self.sync_providers)) or "none"
            raise KeyError(
                f"no sync credential provider registered for kind '{kind}' " f"(known: {known})"
            ) from None

    def kinds(self) -> list[str]:
        """Registered kinds, sorted."""
        return sorted(self.async_providers)


_REGISTRY = _Registry()


def register_provider(
    provider: IAsyncCredentialProvider,
    sync_twin: Any | None = None,
) -> None:
    """Register a provider (and optionally its sync twin) under its kind."""
    _REGISTRY.register(provider, sync_twin)


def get_provider(kind: str) -> IAsyncCredentialProvider:
    """Return the registered async provider for ``kind``."""
    return _REGISTRY.get(kind)


def get_sync_provider(kind: str) -> Any:
    """Return the registered sync provider for ``kind``."""
    return _REGISTRY.get_sync(kind)


def provider_kinds() -> list[str]:
    """Every registered provider kind."""
    return _REGISTRY.kinds()


def parse_credential_ref(value: str) -> tuple[str, str]:
    """Split a ``kind/name`` CLI reference.

    Args:
        value: ``"pat/work"`` style reference.

    Returns:
        ``(kind, name)``.

    Raises:
        ValueError: If the value is not ``kind/name``.
    """
    kind, sep, name = value.partition("/")
    if not sep or not kind.strip() or not name.strip():
        raise ValueError(
            f"invalid credential reference {value!r}; expected 'kind/name', "
            f"for example 'pat/work'"
        )
    return kind.strip(), name.strip()


def normalize_capabilities(raw: Mapping[str, Any]) -> Capabilities:
    """Normalize a client probe into :class:`Capabilities`.

    The PAT client's probe is heuristic (GitHub has no permissions endpoint), so
    this only pins the shape; interpretation stays with the provider.
    """
    return Capabilities(
        is_valid=bool(raw.get("is_valid", False)),
        identity=str(raw.get("username") or raw.get("identity") or "").strip(),
        can_read_actions=bool(raw.get("can_read_actions", False)),
        can_write_actions=bool(raw.get("can_write_actions", False)),
        can_manage_webhooks=bool(raw.get("can_manage_webhooks", False)),
        accessible_repos=int(raw.get("accessible_repos") or 0),
        probed_at=datetime.now(timezone.utc),
    )


__all__ = [
    "Capabilities",
    "IAsyncCredentialProvider",
    "get_sync_provider",
    "ResolvedCredential",
    "get_provider",
    "normalize_capabilities",
    "parse_credential_ref",
    "provider_kinds",
    "register_provider",
]
