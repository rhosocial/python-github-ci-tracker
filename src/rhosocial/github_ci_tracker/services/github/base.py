# src/rhosocial/github_ci_tracker/services/github/base.py
"""Where a group's token comes from, decided once at construction.

The token is not a parameter of any method. Threading it through every call is
what made the old controllers resolve the same credential per request, and made a
caller that forgot read anonymously without saying so.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TokenSource:
    """The order a group resolves a credential in.

    1. an injected credential always wins -- the ambient PAT a long-lived process
       resolved once at startup;
    2. otherwise ``resolve`` decides per repository, which is how the CLI and the
       tests route through ``ct_repository_bindings``;
    3. otherwise the call is anonymous, and a public repository is readable.

    ``resolve`` takes a repository full name rather than a row: a group is given a
    name by its caller, and looking the row up is the credentials package's job.
    """

    credential: Any | None = None
    resolve: Callable[[str], Awaitable[Any | None]] | None = None

    async def token_for(self, repo_full_name: str) -> str | None:
        """The token to use for ``repo_full_name``, or ``None`` for anonymous."""
        if self.credential is not None:
            return getattr(self.credential, "token", None)
        if self.resolve is not None:
            resolved = await self.resolve(repo_full_name)
            return None if resolved is None else getattr(resolved, "token", None)
        return None


__all__ = ["TokenSource"]
