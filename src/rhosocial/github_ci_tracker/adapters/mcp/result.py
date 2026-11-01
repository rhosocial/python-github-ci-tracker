# src/rhosocial/github_ci_tracker/adapters/mcp/result.py
"""Shared response shaping for the MCP tools.

Every tool answers with the same envelope so a model never has to guess what a
failure looks like:

* success — the tool's own payload, plus an optional ``hint``.
* failure — the tool **raises** :class:`MCPToolError`, which the MCP layer turns
  into ``isError: true`` with the JSON envelope as its text. Returning the
  envelope as the result instead would make a failed call look like a successful
  one to a client that only inspects the protocol status.

The envelope carries a machine-readable ``code``, a human ``message``, an
actionable ``hint`` and, when the fix is a settings page, an ``action_url`` that
is absolute when the service knows its own origin
(``CITRACKER_PUBLIC_BASE_URL``, else the request's own ``Host``).

Errors are the part worth pinning down. The rules come from how the service
fails in practice: a repository that is not subscribed, a PAT that was revoked,
a run that GitHub has not scheduled yet. None of those are fixed by asking the
caller for a token -- the user has to act in the tracker's own settings -- so
the message says what to do and never asks for a secret.

This module also holds the tool annotations. ``readOnlyHint`` is what lets a
client show "this only reads" instead of a confirmation dialog; the trigger
tool is explicitly not read-only and not idempotent so the opposite happens.
It is a hint for the UI, never the enforcement: the credential's Actions write
permission is what actually decides.
"""

from __future__ import annotations

import json
import os
from contextvars import ContextVar
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ...core.exceptions import (
    AttemptNotFoundError,
    CitrackerError,
    GithubAPIError,
    InvalidStateTransitionError,
    JobNotFoundError,
    RateLimitError,
    RepositoryNotFoundError,
    RunInProgressError,
    RunNotFoundError,
    TokenDeactivatedError,
    TokenExpiredError,
    TokenInvalidError,
    TokenNotFoundError,
    TokenPermissionError,
    TokenRequiredError,
    TriggerFailedError,
    UpstreamUnavailableError,
    ValidationError,
    WebhookNotFoundError,
    WorkflowNotFoundError,
    is_state_conflict,
)

#: Where a user fixes subscription and credential problems. The REST surface is
#: the canonical one; an installation may front it with a web UI at the same
#: paths.
SETTINGS_PATH = "/api/v1/repositories"

#: The request's origin (:func:`origin`), published per request. A per-request
#: value beats ``CITRACKER_PUBLIC_BASE_URL`` only when that variable is unset:
#: an operator who tells the service where it lives should not be overruled by
#: whatever a caller put in its ``Host`` header.
_request_origin: ContextVar[str] = ContextVar("citracker_request_origin", default="")


def set_request_origin(scheme: str = "", host: str = "") -> None:
    """Publish the scheme and host of the request being served.

    Called once per HTTP request by the MCP app wrapper, so an ``action_url``
    can name the address this very caller used. Both default to empty, which
    clears the value for a scope that has no origin (a background task, or the
    next test).
    """
    _request_origin.set(f"{scheme}://{host}" if host else "")


def origin() -> str:
    """The service's own origin, or "" when nothing names one.

    ``CITRACKER_PUBLIC_BASE_URL`` wins when set, because it is the one value
    that is true behind a proxy that rewrites ``Host``. Without it the request's
    own origin is used, which is correct for a direct connection and wrong only
    where the caller reached a different name than the operator published.
    """
    configured = os.getenv("CITRACKER_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    return _request_origin.get()


def settings_url() -> str:
    """The absolute URL a user opens to add repositories, or "" when unknown."""
    base = origin()
    return f"{base}{SETTINGS_PATH}" if base else ""


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

#: Reading a log is still a read, but it can fetch from GitHub on a cache miss,
#: so it is marked open-world rather than side-effect free on the local cache.
READ_ONLY_FETCHING = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

#: Triggering runs CI: it spends minutes and may deploy.
NOT_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)

#: Re-running is as dangerous as triggering: it needs no ``workflow_dispatch``
#: declaration, so any run -- including a deployment -- can be repeated.
NOT_IDEMPOTENT_RERUN = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)

#: Cancelling changes state but destroys nothing and can be repeated: a second
#: cancel of the same run is a no-op on GitHub. It is listed separately from the
#: trigger pair so a client does not warn about it as if it spent CI minutes.
SAFE_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def _code_for(exc: CitrackerError) -> tuple[str, str, str | None]:
    """Map an exception onto ``(code, hint, action_url)``."""
    if isinstance(exc, RepositoryNotFoundError):
        return (
            "repository_not_subscribed",
            f"Ask the user to add the repository at {settings_url()}, then retry.",
            SETTINGS_PATH,
        )
    if isinstance(exc, (TokenNotFoundError, TokenDeactivatedError, TokenRequiredError)):
        return (
            "credential_missing",
            f"No usable credential is bound to this repository. Ask the user to "
            f"add one at {settings_url()}; never ask the user for a token.",
            SETTINGS_PATH,
        )
    if isinstance(exc, TokenExpiredError):
        return (
            "credential_expired",
            "The bound token has expired. Ask the user to rotate it; do not retry "
            "with the same credential.",
            SETTINGS_PATH,
        )
    if isinstance(exc, TokenInvalidError):
        return (
            "credential_invalid",
            "GitHub rejected the bound token. Ask the user to replace it at " f"{settings_url()}.",
            SETTINGS_PATH,
        )
    if isinstance(exc, RunInProgressError):
        return (
            "run_in_progress",
            "The run is still running, so GitHub refuses to re-run it. This is a "
            "concurrency limit, not a credential problem: wait for it to finish "
            "(wait_for_run or get_run), or cancel it, then re-run. Changing the "
            "token would not help.",
            None,
        )
    if isinstance(exc, TokenPermissionError):
        return (
            "credential_insufficient",
            "The bound token lacks the permission this operation needs. Ask the "
            "user to grant it on GitHub and re-add the token; do not look for "
            "another credential yourself.",
            SETTINGS_PATH,
        )
    if isinstance(exc, AttemptNotFoundError):
        return (
            "attempt_not_found",
            "The run exists but has no such attempt. Attempts are numbered from 1 "
            "and the newest one is reported by get_run without an attempt "
            "argument; call that first if the number is uncertain.",
            None,
        )
    if isinstance(exc, RunNotFoundError):
        return (
            "run_not_found",
            "The run is unknown locally and to GitHub. Check the run id, or call "
            "find_runs for this repository to list what exists.",
            None,
        )
    if isinstance(exc, JobNotFoundError):
        return (
            "job_not_found",
            "The job is not stored. A job id is only meaningful inside its run, "
            "so call get_run(run_id, include_jobs=true) for the run it belongs to "
            "first; if you do not have the run id, find_runs finds it.",
            None,
        )
    if isinstance(exc, WorkflowNotFoundError):
        return (
            "workflow_not_found",
            "The error lists every workflow this repository has, as "
            "'name (path)'. Pass either the name or the file path exactly as "
            "listed; do not guess a variant spelling.",
            None,
        )
    if isinstance(exc, UpstreamUnavailableError):
        return (
            "upstream_unavailable",
            "GitHub did not answer. Nothing about your request is wrong, so do "
            "not change the arguments: retry once. If it keeps happening, the "
            "deployment's route to api.github.com is the problem -- report that, "
            "not the tool call.",
            None,
        )
    if isinstance(exc, RateLimitError):
        return (
            "rate_limited",
            "GitHub's rate limit is exhausted for the bound token. Wait before "
            "retrying; prefer cached reads in the meantime.",
            None,
        )
    if isinstance(exc, TriggerFailedError):
        if "workflow_dispatch" in str(exc).lower():
            # The workflow resolved and GitHub received the dispatch, then
            # refused it because its trigger block has no workflow_dispatch.
            # This is not a name problem, and no run was created, so the usual
            # "check find_runs before retrying" advice would be noise.
            return (
                "workflow_not_dispatchable",
                "The workflow exists but declares no 'workflow_dispatch' trigger, so "
                "GitHub will not start it on demand. No run was created. Push a "
                "commit to the ref instead, or add 'on: workflow_dispatch' to the "
                "workflow file; a different spelling of the name changes nothing.",
                None,
            )
        return (
            "trigger_failed",
            "GitHub rejected or did not materialize the dispatch. Before retrying, "
            "call find_runs for this ref: the run may exist already, and dispatching "
            "again would start a second one.",
            None,
        )
    if isinstance(exc, WebhookNotFoundError):
        return (
            "webhook_not_found",
            "This repository has no webhook managed by the tracker. Ask the user to "
            f"create one at {settings_url()} if push updates are expected.",
            SETTINGS_PATH,
        )
    if isinstance(exc, InvalidStateTransitionError):
        return (
            "invalid_state_transition",
            "The stored record is ahead of the incoming update. Call get_run with "
            "Call find_runs (which reads GitHub) to reconcile it.",
            None,
        )
    if isinstance(exc, ValidationError):
        return ("invalid_argument", str(exc) or "The arguments are not valid.", None)
    if isinstance(exc, GithubAPIError):
        status = getattr(exc, "status_code", None)
        if status in (403, 409) and is_state_conflict(str(exc)):
            # A transport error that never went through map_api_error: same
            # concurrency, same answer -- do not send the caller to the token.
            return (
                "run_in_progress",
                "The run is still running, so GitHub refuses to re-run it. This is "
                "a concurrency limit, not a credential problem: wait for it to "
                "finish, or cancel it, then re-run.",
                None,
            )
        if status == 403:
            return (
                "github_forbidden",
                "GitHub refused the request for this credential. The token may lack "
                f"the required permission; ask the user to check it at {settings_url()}.",
                SETTINGS_PATH,
            )
        if status == 409:
            return (
                "run_state_conflict",
                "The run's current state does not allow this operation (it may "
                "already be finished). Call get_run to see where it stands; "
                "retrying unchanged will fail the same way.",
                None,
            )
        if status == 404:
            return (
                "github_not_found",
                "GitHub does not expose this resource to the bound credential, or it "
                "no longer exists.",
                None,
            )
        return (
            "github_api_error",
            "GitHub returned an error. Retry once; if it persists, report the "
            "request path and status to the user.",
            None,
        )
    return ("internal_error", "The operation failed unexpectedly. Report it to the user.", None)


def error_result(exc: Exception) -> dict[str, Any]:
    """Shape an exception into the tool error envelope.

    Args:
        exc: The exception a tool caught.

    Returns:
        ``{"error": {"code", "message", "hint", "action_url"?}, "isError": True}``.
    """
    if isinstance(exc, MCPToolError):
        # Already shaped: re-wrapping would bury the original code under
        # "internal_error" and the JSON text as the message.
        return exc.payload

    if isinstance(exc, CitrackerError):
        code, hint, action_url = _code_for(exc)
        message = str(exc) or exc.__class__.__name__
    else:
        code, hint, action_url = (
            "internal_error",
            "The operation failed unexpectedly. Report it to the user.",
            None,
        )
        message = f"{exc.__class__.__name__}: {exc}"

    error: dict[str, Any] = {"code": code, "message": message, "hint": hint}
    if action_url:
        error["action_url"] = _absolute(action_url)
    return {"error": error, "isError": True}


def _absolute(path: str) -> str:
    """Expand a settings path against the service origin when one is known.

    An unusable relative path is filtered out rather than published: the whole
    point of the field is that a client can open it, so a value that cannot be
    opened is worse than its absence.
    """
    if path.startswith("http://") or path.startswith("https://"):
        return path
    base = origin()
    return f"{base}{path}" if base else ""


class MCPToolError(ToolError):
    """An error raised by a tool so the MCP layer marks the call failed.

    Two problems it solves at once. Returning ``{"error": ...}`` as the result
    text makes a failed call look like a successful one to a client that only
    inspects the protocol status; raising a bare exception loses the
    machine-readable code and the hint, because the SDK reports only
    ``Error executing tool <name>`` for anything that is not a :class:`ToolError`.
    Subclassing ``ToolError`` keeps the message on the wire, and ``str()`` is the
    JSON envelope the model reads.
    """

    def __init__(self, exc: Exception) -> None:
        payload = error_result(exc)
        super().__init__(json.dumps(payload))
        self.payload = payload


def error_envelope(exc: Exception) -> MCPToolError:
    """Wrap ``exc`` in the exception a tool raises instead of returning."""
    if isinstance(exc, MCPToolError):
        # A tool's own fallback may re-catch what it raised; keep the first
        # envelope rather than wrapping the wrapper.
        return exc
    return MCPToolError(exc)


def duration_block(sample: Any) -> dict[str, Any] | None:
    """Render a workflow's recent durations, or ``None`` when there is no history.

    A fresh subscription has no finished run to sample. Answering ``null`` is the
    honest result there: a budget invented for a workflow nobody has run yet
    would be read as evidence.
    """
    if sample is None:
        return None
    return {
        "samples": sample.samples,
        "p50_seconds": round(sample.p50_seconds, 1),
        "p90_seconds": round(sample.p90_seconds, 1),
    }


def with_hint(payload: dict[str, Any], hint: str | None) -> dict[str, Any]:
    """Attach a ``hint`` to a payload, leaving it untouched when empty."""
    if hint:
        payload = {**payload, "hint": hint}
    return payload


def paged(
    items: list[Any],
    *,
    limit: int,
    offset: int,
    total: int | None = None,
    hint: str | None = None,
) -> dict[str, Any]:
    """Shape a page of results with explicit truncation information.

    ``total`` is a "there is more" signal, not an exact count: counting every
    row to report a number would defeat the point of paging. It is derived from
    whether the caller filled the page.

    Args:
        items: The rows on this page.
        limit: The page size that was requested.
        offset: The offset that was requested.
        total: Optional authoritative total, when cheaply available.
        hint: Optional next-step hint.

    Returns:
        The page envelope.
    """
    payload: dict[str, Any] = {
        "items": items,
        "returned": len(items),
        "offset": offset,
        "limit": limit,
        "truncated": len(items) >= limit and (total is None or offset + len(items) < total),
    }
    if total is not None:
        payload["total"] = total
    if payload["truncated"]:
        payload["next_offset"] = offset + len(items)
        payload["hint"] = hint or (
            f"More results exist. Call again with offset={offset + len(items)}."
        )
    elif hint:
        payload["hint"] = hint
    return payload


__all__ = [
    "MCPToolError",
    "NOT_IDEMPOTENT",
    "NOT_IDEMPOTENT_RERUN",
    "SAFE_WRITE",
    "READ_ONLY",
    "READ_ONLY_FETCHING",
    "SETTINGS_PATH",
    "error_envelope",
    "error_result",
    "origin",
    "paged",
    "set_request_origin",
    "settings_url",
    "with_hint",
]
