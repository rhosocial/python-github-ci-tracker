# src/rhosocial/github_ci_tracker/services/__init__.py
"""The service layer: capabilities, one object per resource.

A service is a capability this system reaches for -- today that is GitHub, and
only GitHub. :mod:`~.github` names the protocols a caller can be typed against
(``RunReader``, ``RunControl``, ``WebhookManager``, ``CredentialProbe``) and ships
one implementation of each over the HTTP client. A controller declares which
protocol it needs and is handed anything that satisfies it: the HTTP group in
production, a recording fake in a test.

What is *not* here is the point of this package. There is no use case, no
coordination between models and no storage: those are controllers
(:mod:`rhosocial.github_ci_tracker.controllers`), and a flow that lives here
would be a second implementation of one that already lives there.

Adding a capability -- a second provider, an S3 mirror, a cache in front of the
API -- means adding one module here. Nothing above this package changes.
"""

from __future__ import annotations

from .github import (
    CredentialProbe,
    GithubCredentials,
    GithubRepositories,
    GithubRuns,
    GithubWebhooks,
    RepositoryReader,
    RunControl,
    RunReader,
    TokenSource,
    WebhookManager,
)

__all__ = [
    "CredentialProbe",
    "GithubCredentials",
    "GithubRepositories",
    "GithubRuns",
    "GithubWebhooks",
    "RepositoryReader",
    "RunControl",
    "RunReader",
    "TokenSource",
    "WebhookManager",
]
