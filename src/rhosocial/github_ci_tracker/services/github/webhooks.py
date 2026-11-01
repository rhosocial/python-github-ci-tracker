# src/rhosocial/github_ci_tracker/services/github/webhooks.py
"""Webhooks: the hooks this service owns on a repository.

The push channel exists because GitHub delivers what this service never asked for.
These four verbs are how it is armed and disarmed, and the reason ``delete`` is a
separate capability from ``create``: an unsubscribing repository must remove only
the hooks this service created, which means listing first and comparing the URL --
a rule that lives in the caller, because it needs to know what "ours" means.

Payloads come back unchanged; the hook config is read by the caller that created
it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .base import TokenSource
from .client import AsyncGithubClient


def _default_client() -> AsyncGithubClient:
    """The HTTP client, built once per group."""
    return AsyncGithubClient()


class GithubWebhooks:
    """``/repos/{owner}/{repo}/hooks``."""

    def __init__(
        self,
        client: AsyncGithubClient | None = None,
        *,
        credential: Any | None = None,
        resolve: Callable[[str], Awaitable[Any | None]] | None = None,
    ) -> None:
        """Build the group.

        Args:
            client: Injected transport; a default HTTP client when omitted.
            credential: The ambient credential, when the process has one.
            resolve: Async callable ``repo_full_name -> credential | None`` used
                when there is no ambient credential.
        """
        self._client = client if client is not None else _default_client()
        self._tokens = TokenSource(credential=credential, resolve=resolve)

    async def create_webhook(
        self, repo_full_name: str, config: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Create a hook and return what GitHub stored.

        ``config`` is the hook body as GitHub takes it (``url``, ``secret``,
        ``events``, ``content_type``), not a parsed form: the caller owns the
        decision of what to subscribe to.

        Raises:
            WebhookAlreadyExistsError: A hook with that URL is already there --
                mapped by the transport from a 422.
        """
        return await self._client.create_webhook(
            token=await self._tokens.token_for(repo_full_name),
            repo_full_name=repo_full_name,
            config=dict(config),
        )

    async def list_webhooks(self, repo_full_name: str) -> list[Mapping[str, Any]]:
        """List the repository's hooks.

        Every caller that has to identify "our" hook starts here, so this is the
        one read the group exposes.
        """
        return await self._client.list_webhooks(
            token=await self._tokens.token_for(repo_full_name),
            repo_full_name=repo_full_name,
        )

    async def delete_webhook(self, repo_full_name: str, hook_id: int) -> None:
        """Delete one hook by id."""
        await self._client.delete_webhook(
            token=await self._tokens.token_for(repo_full_name),
            repo_full_name=repo_full_name,
            hook_id=hook_id,
        )

    async def ping_webhook(self, repo_full_name: str, hook_id: int) -> None:
        """Ask GitHub to deliver a ``ping`` so the operator can see the channel work."""
        await self._client.ping_webhook(
            token=await self._tokens.token_for(repo_full_name),
            repo_full_name=repo_full_name,
            hook_id=hook_id,
        )


__all__ = ["GithubWebhooks"]
