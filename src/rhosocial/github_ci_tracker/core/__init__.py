# src/rhosocial/github_ci_tracker/core/__init__.py
"""The static definitions the whole service shares -- and nothing that acts.

What remains in this package is what has no dependency on a layer above or
below it: the exception hierarchy, the state machine's vocabulary, the GitHub
client protocol, and the run tracker. Everything that *does* something moved
out, because a module that acts needs to know who it acts on:

* the flows are in :mod:`rhosocial.github_ci_tracker.controllers`;
* the capabilities (the GitHub API) are in
  :mod:`rhosocial.github_ci_tracker.services`;
* the composition root is ``container.py`` at the package root.

``core`` imports only models and the standard library. Nothing in it imports a
service, a controller or an adapter, which is why it can be imported first.
"""

from __future__ import annotations

from ..models.states import (
    JOB_TRANSITIONS,
    KIND_JOB,
    KIND_RUN,
    KIND_STEP,
    RUN_TRANSITIONS,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_QUEUED,
    STEP_TRANSITIONS,
    TRANSITIONS_BY_KIND,
    VALID_CONCLUSIONS,
    StateMachine,
    normalize_conclusion,
    normalize_status,
    parse_timestamp,
)
from .exceptions import (
    AttemptNotFoundError,
    CitrackerError,
    ConcurrentUpdateError,
    GithubAPIError,
    InvalidStateTransitionError,
    JobNotFoundError,
    RateLimitError,
    RepositoryNotFoundError,
    RunInProgressError,
    RunNotFoundError,
    StepNotFoundError,
    TokenInUseError,
    TokenInvalidError,
    TokenNotFoundError,
    TokenPermissionError,
    TriggerFailedError,
    UpstreamUnavailableError,
    ValidationError,
    WebhookAlreadyExistsError,
    WebhookNotFoundError,
    WorkflowNotFoundError,
    map_api_error,
)
from .interfaces import IGithubClient
from .run_tracker import RunTracker

__all__ = [
    # Exceptions
    "CitrackerError",
    "AttemptNotFoundError",
    "ConcurrentUpdateError",
    "GithubAPIError",
    "InvalidStateTransitionError",
    "JobNotFoundError",
    "RateLimitError",
    "RepositoryNotFoundError",
    "RunInProgressError",
    "RunNotFoundError",
    "StepNotFoundError",
    "TokenInUseError",
    "TokenInvalidError",
    "TokenNotFoundError",
    "TokenPermissionError",
    "TriggerFailedError",
    "UpstreamUnavailableError",
    "ValidationError",
    "WebhookAlreadyExistsError",
    "WebhookNotFoundError",
    "WorkflowNotFoundError",
    "map_api_error",
    # Support components
    "RunTracker",
    "StateMachine",
    "IGithubClient",
    # State machine data
    "STATUS_QUEUED",
    "STATUS_IN_PROGRESS",
    "STATUS_COMPLETED",
    "KIND_RUN",
    "KIND_JOB",
    "KIND_STEP",
    "RUN_TRANSITIONS",
    "JOB_TRANSITIONS",
    "STEP_TRANSITIONS",
    "TRANSITIONS_BY_KIND",
    "VALID_CONCLUSIONS",
    "normalize_status",
    "normalize_conclusion",
    "parse_timestamp",
]
