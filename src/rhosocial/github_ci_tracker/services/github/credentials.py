# src/rhosocial/github_ci_tracker/services/github/credentials.py
"""Credentials: ask GitHub what a token is, and what it may do.

Both questions are about a *token*, not about a repository, so a token is what
these methods take. That is the opposite of the other groups: `GithubRuns` reads
for a repository and lets `TokenSource` decide which credential answers, while
here the credential is the subject of the question and passing it is the point.

`probe_actions_write` is the exception: it asks whether *the credential that
answers for this repository* may dispatch there, which is a per-repository
question again.
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


class GithubCredentials:
    """``/user`` and the dispatch probe."""

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

    async def verify_token(self, token: str, probe_repo: str | None = None) -> Mapping[str, Any]:
        """Describe a token by asking GitHub about it.

        The token is a parameter because it is the subject: a caller checking a
        token it is about to store has one that is not yet any repository's
        credential. The probe reports what GitHub answered -- identity, scopes,
        expiry, whether it may write actions -- and nothing is inferred.

        Args:
            token: The raw token to probe.
            probe_repo: A repository to test ``actions: write`` against. Without
                one, the write capability cannot be measured and is reported as
                unknown rather than guessed.

        Raises:
            TokenInvalidError: GitHub rejected the token.
        """
        return await self._client.verify_token(token, probe_repo)

    async def probe_actions_write(self, repo_full_name: str) -> bool:
        """Whether the credential answering for ``repo_full_name`` may dispatch.

        Measured, not configured: a read-only token can never report write
        capability, no matter what an operator or a model asks for.

        Raises:
            TokenRequiredError: The repository has no credential and no ambient
                one is configured -- an anonymous probe cannot answer this.
        """
        token = await self._tokens.token_for(repo_full_name)
        if token is None:
            raise TokenRequiredError(
                f"Repository '{repo_full_name}' has no credential to probe; "
                "bind one before asking about write capability",
                repository=repo_full_name,
            )
        return await self._client.probe_actions_write(token, repo_full_name)


__all__ = ["GithubCredentials"]
