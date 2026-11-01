# src/rhosocial/github_ci_tracker/services/github/repositories.py
"""Repositories and their workflows, straight from the API.

Payloads go back unchanged: ``AsyncRepository.upsert_from_remote`` and
``AsyncWorkflow.upsert_from_remote`` already read them, and a second reader here
would be a copy that drifts.

``list_accessible_repos`` is the one method that takes a credential. It asks what
*that* credential can see, so the credential is the subject of the call rather
than ambient context -- passing it is the point, not bookkeeping.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ...core.exceptions import TokenRequiredError
from .base import TokenSource
from .client import AsyncGithubClient


def _default_client() -> AsyncGithubClient:
    """The HTTP client, built once per group."""
    return AsyncGithubClient()


class GithubRepositories:
    """``/repos``: metadata, the workflow list, and credential-scoped discovery."""

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

    async def get_repo_info(
        self, repo_full_name: str, credential: Any | None = None
    ) -> Mapping[str, Any]:
        """Fetch one repository's metadata.

        ``credential`` overrides the ambient one. Subscribing asks a specific
        credential whether it can see the repository -- the answer is about that
        credential, so it is the subject of the call rather than ambient context.
        """
        return await self._client.get_repo_info(
            token=(
                credential.token
                if credential is not None
                else await self._tokens.token_for(repo_full_name)
            ),
            repo_full_name=repo_full_name,
        )

    async def get_repo_workflows(self, repo_full_name: str) -> list[Mapping[str, Any]]:
        """List a repository's workflows, disabled ones included.

        ``state`` is what tells them apart; the cache decides what to keep, not
        this call.
        """
        return await self._client.get_repo_workflows(
            token=await self._tokens.token_for(repo_full_name),
            repo_full_name=repo_full_name,
        )

    async def get_pull_request(self, repo_full_name: str, number: int) -> Mapping[str, Any]:
        """Fetch one pull request.

        The runs endpoint has no PR filter, so the lookup starts here: the
        pull request names its ``head.sha``, and the runs are then read by
        commit. That attribution is the service's job; this call just returns
        what GitHub says.
        """
        return await self._client.get_pull_request(
            await self._tokens.token_for(repo_full_name),
            repo_full_name,
            number,
        )

    async def list_accessible_repos(self, credential: Any | None = None) -> list[Mapping[str, Any]]:
        """List the repositories a credential can see.

        Args:
            credential: The credential to ask about. Falls back to the ambient
                one; with neither there is nothing to ask, which is what
                :class:`TokenRequiredError` says -- an anonymous ``/user/repos``
                would answer 401 and read as a GitHub failure.

        Raises:
            TokenRequiredError: No credential was supplied and none is ambient.
        """
        credential = credential if credential is not None else self._tokens.credential
        if credential is None:
            raise TokenRequiredError(
                "Listing accessible repositories needs a credential: pass one, or "
                "construct the group with an ambient credential",
            )
        return await self._client.list_accessible_repos(token=credential.token)


__all__ = ["GithubRepositories"]
