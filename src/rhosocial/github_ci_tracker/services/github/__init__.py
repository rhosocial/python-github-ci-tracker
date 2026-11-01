# src/rhosocial/github_ci_tracker/services/github/__init__.py
"""GitHub as a service: capability protocols, and one object per group.

A service names the protocol it needs and is handed anything that satisfies it --
the implementation here in production, a recording fake in a test. The protocols
are structural, so a fake does not inherit behaviour it will not use, and a reader
typed against ``RunReader`` cannot dispatch a workflow.

The grouping is by resource, which is the unit GitHub itself uses:

* :class:`~.runs.GithubRuns` -- ``/actions/runs``, reads and the three verbs that
  spend CI minutes;
* :class:`~.repositories.GithubRepositories` -- ``/repos``, metadata, workflows
  and credential-scoped discovery.

Payloads go back as ``Mapping`` rather than as a hand-written object: the models
already parse them, and a second parser would be a copy that drifts.
"""

from __future__ import annotations

from .base import TokenSource
from .credentials import GithubCredentials
from .protocols import (
    CredentialProbe,
    RepositoryReader,
    RunControl,
    RunReader,
    WebhookManager,
)
from .repositories import GithubRepositories
from .runs import GithubRuns
from .webhooks import GithubWebhooks

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
