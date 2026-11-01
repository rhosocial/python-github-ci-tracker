# src/rhosocial/github_ci_tracker/models/errors.py
"""The errors a model raises on its own.

A model may not import anything outside this directory, so the errors it raises
have to be defined here. That set is deliberately small: the state machine
refuses an illegal transition, and a local workflow lookup comes up empty.
Everything else -- tokens, triggers, webhooks, the GitHub API -- is decided by a
service, and those errors live above this layer.

:class:`CitrackerError` sits here for the same reason: it is the root the whole
vocabulary hangs off, and a model cannot reach up for it.
"""

from __future__ import annotations


class CitrackerError(Exception):
    """Base exception for all business logic errors."""

    pass


class InvalidStateTransitionError(CitrackerError):
    """Invalid state machine transition attempted."""

    pass


class WorkflowNotFoundError(CitrackerError):
    """Workflow not found in repository."""

    pass


__all__ = [
    "CitrackerError",
    "InvalidStateTransitionError",
    "WorkflowNotFoundError",
]
