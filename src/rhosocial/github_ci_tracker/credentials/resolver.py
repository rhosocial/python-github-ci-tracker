# src/rhosocial/github_ci_tracker/credentials/resolver.py
"""Which credential answers for a repository.

A repository row holds an opaque ``(provider_kind, provider_ref)`` pair in
``ct_repository_bindings``; this module turns that pair into a usable credential
by asking the registry for the provider. The routing rule is written once here,
in both twins, instead of on the model -- which is what used to happen, and what
made ``models`` depend on the provider registry.

Anonymous is a legitimate answer: a repository with no binding is read with
GitHub's anonymous rate limit. ``require_*`` is for the operations that cannot
proceed without authorization (dispatching, managing hooks) and says so.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..core.exceptions import TokenRequiredError
from ..models.binding import AsyncRepositoryBinding, RepositoryBinding
from .base import ResolvedCredential, get_provider, get_sync_provider

if TYPE_CHECKING:
    from ..models.repository import AsyncRepository, Repository

logger = logging.getLogger(__name__)

#: The message both ``require_*`` helpers raise; it names the cure, not the cause.
_NO_BINDING = (
    "Repository '{full_name}' has no bound credential; "
    "bind one before triggering or managing webhooks"
)


async def resolve_for_repository(
    repo: AsyncRepository | Repository,
) -> ResolvedCredential | None:
    """Return the credential that answers for ``repo``, or ``None`` if anonymous.

    A repository with no binding reads anonymously. That is not an error: the
    operator may have declared a public repository readable without a credential,
    accepting the anonymous rate limit and the no-trigger rule.

    Args:
        repo: The repository subscription.

    Returns:
        The resolved credential, or ``None`` for an anonymous subscription.

    Raises:
        TokenNotFoundError: The bound credential no longer exists.
        TokenDeactivatedError: It exists but was deactivated.
        TokenExpiredError: It is past its declared expiry.
        KeyError: The binding names a provider this build does not have.
    """
    if repo.id is None:
        return None

    binding = await AsyncRepositoryBinding.for_repository(repo.id)
    if binding is None:
        return None

    provider = get_provider(binding.provider_kind)
    return await provider.resolve(binding.provider_ref, repository=repo.full_name)


def resolve_for_repository_sync(repo: Repository) -> ResolvedCredential | None:
    """Synchronous twin of :func:`resolve_for_repository`."""
    if repo.id is None:
        return None

    binding = RepositoryBinding.for_repository(repo.id)
    if binding is None:
        return None

    return get_sync_provider(binding.provider_kind).resolve(
        binding.provider_ref, repository=repo.full_name
    )


async def require_for_repository(repo: AsyncRepository | Repository) -> ResolvedCredential:
    """Like :func:`resolve_for_repository` but refuse an anonymous subscription.

    Raises:
        TokenRequiredError: The repository has no bound credential, so an
            operation needing authorization cannot run.
    """
    credential = await resolve_for_repository(repo)
    if credential is None:
        raise TokenRequiredError(
            _NO_BINDING.format(full_name=repo.full_name), repository=repo.full_name
        )
    return credential


def require_for_repository_sync(repo: Repository) -> ResolvedCredential:
    """Synchronous twin of :func:`require_for_repository`."""
    credential = resolve_for_repository_sync(repo)
    if credential is None:
        raise TokenRequiredError(
            _NO_BINDING.format(full_name=repo.full_name), repository=repo.full_name
        )
    return credential


async def binding_of(repo: AsyncRepository | Repository) -> AsyncRepositoryBinding | None:
    """Return ``repo``'s binding row, or ``None`` for an anonymous subscription."""
    if repo.id is None:
        return None
    return await AsyncRepositoryBinding.for_repository(repo.id)


async def bind_credential(
    repository_id: int,
    kind: str,
    ref: int,
) -> AsyncRepositoryBinding:
    """Bind a repository to a credential, replacing any previous binding."""
    return await AsyncRepositoryBinding.bind(repository_id, kind, ref)


async def unbind_credential(repository_id: int) -> bool:
    """Drop a repository's binding; ``True`` when one existed."""
    return await AsyncRepositoryBinding.unbind(repository_id)


async def credential_summary(repo: AsyncRepository | Repository) -> dict[str, Any] | None:
    """Describe ``repo``'s credential for protocol output, without the secret."""
    binding = await binding_of(repo)
    if binding is None:
        return None

    try:
        provider = get_provider(binding.provider_kind)
        described = await provider.describe(binding.provider_ref)
    except Exception as exc:  # noqa: BLE001 - a missing provider is reported, not raised
        return {
            **binding.describe(),
            "available": False,
            "error": f"{exc.__class__.__name__}: {exc}",
        }
    return {**binding.describe(), "available": True, **described}


__all__ = [
    "bind_credential",
    "binding_of",
    "credential_summary",
    "require_for_repository",
    "require_for_repository_sync",
    "resolve_for_repository",
    "resolve_for_repository_sync",
    "unbind_credential",
]
