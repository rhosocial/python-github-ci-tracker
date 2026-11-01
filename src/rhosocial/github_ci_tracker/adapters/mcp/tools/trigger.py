# src/rhosocial/github_ci_tracker/adapters/mcp/tools/trigger.py
"""Trigger MCP tools: the only tools that cost CI minutes.

``trigger_workflow`` and ``rerun_run`` are the dangerous pair. Two things keep
them from being a foot-gun:

* The caller is told, in the description and again in the result, that a
  dispatch is not idempotent. When the response is ambiguous the model is told
  to call ``find_runs`` first rather than dispatch twice -- a retry after a
  timeout starts a second run, and for a deployment workflow that is a real
  incident.
* Nothing here trusts the model's judgement about *whether* to trigger. The
  permission answer comes from the bound credential (``can_trigger``) and the
  workflow must exist in the repository's own list; a workflow that is not
  there is refused with the list of what is.

Rerunning is gated identically rather than left open: ``rerun`` needs no
``workflow_dispatch`` declaration, so it can repeat any run, deployments
included.
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from ..result import (
    NOT_IDEMPOTENT,
    NOT_IDEMPOTENT_RERUN,
    SAFE_WRITE,
    duration_block,
    error_envelope,
    with_hint,
)


def register_trigger_tools(server: MCPServer, container: Any) -> None:
    """Register the trigger MCP tools."""

    @server.tool(
        name="trigger_workflow",
        description=(
            "Dispatch a GitHub Actions workflow and register the run it creates. "
            "This spends CI minutes and may deploy: call it only when the user "
            "explicitly asked for a run on this workflow and ref. It is NOT "
            "idempotent -- if the call times out or the outcome is unclear, call "
            "find_runs for this ref before retrying, because a retry starts a "
            "second run. The workflow must declare workflow_dispatch and must "
            "already be known to this service. Pass if_absent=true when the goal "
            'is "this commit has run" rather than "start another run": it '
            "returns the existing run for the ref's head commit instead of "
            "spending CI minutes a second time."
        ),
        annotations=NOT_IDEMPOTENT,
    )
    async def trigger_workflow(
        repository: str,
        workflow: str,
        ref: str,
        inputs: dict[str, str] | None = None,
        if_absent: bool = False,
        commit_sha: str | None = None,
    ) -> dict[str, Any]:
        """Trigger a workflow run.

        Args:
            repository: ``owner/repo``.
            workflow: Workflow path or file name (stable), or its numeric id; a
                display name does not resolve, because it differs per branch. It must exist in
                the repository's workflow list.
            ref: Branch or tag to run on.
            inputs: ``workflow_dispatch`` inputs, as strings.
            if_absent: Return the run this workflow already has for the ref's
                head commit, when there is one, instead of dispatching again.
                The answer says ``dispatched: false`` in that case.
            commit_sha: The commit ``if_absent`` checks. Read from the ref's head
                when omitted; pass it when the credential cannot read branches
                (Actions-only tokens) -- the error says so.

        Returns:
            The run's id, and whether this call started it.
        """
        try:
            # Resolving the repository, the workflow and its credential happens
            # once, in the service. Re-checking it here would create a second
            # source of truth that drifts from the first.
            result = await container.trigger.trigger_workflow(
                repository=repository,
                workflow=workflow,
                ref=ref,
                inputs=inputs,
                if_absent=if_absent,
                commit_sha=commit_sha,
            )
        except Exception as exc:  # noqa: BLE001 - shaped for the caller
            raise error_envelope(exc) from exc

        return with_hint(
            {
                "run_id": result.run_id,
                "repository": repository,
                "workflow": workflow,
                "ref": ref,
                "run_attempt": result.attempt or 1,
                "status": result.status,
                "dispatched": result.dispatched,
                "html_url": result.html_url,
                "typical_duration_seconds": duration_block(result.duration_sample),
                "message": result.message,
            },
            (
                "A run already existed for this ref's head, so nothing was "
                "dispatched and no CI minutes were spent. Call wait_for_run with "
                "this run_id; do not dispatch again."
                if not result.dispatched
                else (
                    "The run exists now. Call wait_for_run to wait for it, or "
                    "get_run for its jobs. Do not dispatch again for this ref."
                )
            ),
        )

    @server.tool(
        name="rerun_run",
        description=(
            "Re-run an existing workflow run. This spends CI minutes and repeats "
            "whatever the original run did -- including a deployment -- so call it "
            "only when the user asked for it. It is NOT idempotent: check get_run "
            "first, and do not retry blindly after an unclear response. A re-run "
            "keeps the same run_id and starts a new attempt (run_attempt + 1), so "
            "the jobs you see afterwards belong to the new attempt."
        ),
        annotations=NOT_IDEMPOTENT_RERUN,
    )
    async def rerun_run(
        repository: str,
        run_id: int,
    ) -> dict[str, Any]:
        """Re-run a workflow run.

        Args:
            repository: ``owner/repo``.
            run_id: GitHub run id to re-run.

        Returns:
            The run that was re-triggered.
        """
        try:
            tokens = await container.token.list_tokens()
            if not any(token["is_active"] for token in tokens):
                raise error_envelope(_credential_error(repository))

            trigger = container.trigger
            result = await trigger.rerun_workflow(repository=repository, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 - shaped for the caller
            raise error_envelope(exc) from exc

        attempt = result.attempt or 0
        previous = result.previous_attempt or 0
        advanced = attempt > previous

        return with_hint(
            {
                "run_id": result.run_id,
                "repository": repository,
                "run_attempt": attempt or None,
                "previous_attempt": previous or None,
                "advanced": advanced,
                "status": result.status,
                "html_url": result.html_url,
                "typical_duration_seconds": duration_block(result.duration_sample),
            },
            (
                f"This is attempt {attempt} of run {result.run_id}; the jobs you "
                "see from now on belong to it, and earlier attempts stay stored. "
                "Call wait_for_run with this run_id. Do not rerun again for the "
                "same run."
                if advanced
                else (
                    "GitHub accepted the re-run but still reports the old attempt, "
                    "so the new one is not visible yet. Call get_run in a few "
                    "seconds to confirm; do not rerun again for the same run."
                )
            ),
        )

    @server.tool(
        name="cancel_run",
        description=(
            "Cancel a queued or in-progress run. Use it when a push superseded a "
            "run that is still burning CI minutes -- cheaper than letting a wide "
            "matrix finish work nobody will read. Cancellation is asynchronous: "
            "GitHub acknowledges the request and the runner stops a moment later, "
            "so the returned status may still be in_progress. Safe to repeat."
        ),
        annotations=SAFE_WRITE,
    )
    async def cancel_run(
        repository: str,
        run_id: int,
    ) -> dict[str, Any]:
        """Cancel a workflow run.

        Args:
            repository: ``owner/repo``.
            run_id: GitHub run id to cancel.

        Returns:
            The run state GitHub reports right after the request.
        """
        try:
            tokens = await container.token.list_tokens()
            if not any(token["is_active"] for token in tokens):
                raise error_envelope(_credential_error(repository))

            trigger = container.trigger
            result = await trigger.cancel_run(repository=repository, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 - shaped for the caller
            raise error_envelope(exc) from exc

        return with_hint(
            {
                "run_id": result.run_id,
                "repository": repository,
                "status": result.status,
                "message": result.message,
                "html_url": result.html_url,
            },
            "Cancellation is asynchronous. Call get_run after a few seconds to "
            "confirm the final state; do not cancel again.",
        )


def _credential_error(repository: str) -> Exception:
    """Build the error raised when no active credential exists."""
    from ....core.exceptions import TokenNotFoundError

    return TokenNotFoundError(
        f"No active credential is bound to '{repository}'; triggering is unavailable."
    )
