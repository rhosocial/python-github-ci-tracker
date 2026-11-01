# src/rhosocial/github_ci_tracker/controllers/trigger.py
"""The write path, in the vocabulary a caller uses.

Speaks ``owner/repo``, a workflow name, a ref, a run id, and answers with the
view objects the adapters serialize. The work is done by two collaborators:

* :class:`~.dispatch.DispatchController` starts, repeats and stops runs;
* :class:`~.run.RunController` reads one back and stores it.

This class owns neither GitHub nor the database, so what is left here is the
translation -- and :meth:`TriggerController.wait_for_completion`, which is a
sequence of reads rather than a source of truth.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..core.exceptions import TokenPermissionError, ValidationError
from ..views import DurationSample, RunView, RunWaitView, _elapsed_seconds, _run_url
from .dispatch import DispatchController
from .run import RunController

if TYPE_CHECKING:
    from ..models import AsyncWorkflowRun

logger = logging.getLogger(__name__)


class TriggerController:
    """Trigger, re-run and cancel runs, and wait for one to finish."""

    def __init__(
        self,
        *,
        dispatch: DispatchController,
        runs: RunController,
    ) -> None:
        """Build the controller.

        Args:
            dispatch: The three verbs that spend CI minutes.
            runs: The read path, used by :meth:`wait_for_completion` to refresh
                the run at both ends of the wait.
        """
        self._dispatch = dispatch
        self._runs = runs

    async def trigger_workflow(
        self,
        repository: str,
        workflow: str,
        ref: str,
        inputs: dict[str, str] | None = None,
        *,
        if_absent: bool = False,
        commit_sha: str | None = None,
    ) -> RunView:
        """Trigger a workflow run.

        Args:
            repository: ``owner/repo``.
            workflow: Workflow file name, path, display name or numeric id.
            ref: Branch or tag to run on.
            inputs: Optional ``workflow_dispatch`` inputs.
            if_absent: Ask for "make sure this commit has run", not "start
                another run". When this workflow already has a run for that
                commit, that run is returned and nothing is dispatched. Off by
                default: a caller that says "trigger" gets a trigger, and one
                that wants the guard says so.
            commit_sha: The commit ``if_absent`` checks. Defaults to the ref's
                head, which needs Contents read on the credential; passing the
                sha the caller already has avoids that read entirely.

        Returns:
            A :class:`RunView` carrying the run id, and ``dispatched=False``
            when an existing run was reused.

        Raises:
            RepositoryNotFoundError: The repository is not tracked.
            WorkflowNotFoundError: The workflow is unknown locally.
            TriggerFailedError: GitHub refused the dispatch, or the run it
                created could not be resolved.
        """
        if if_absent:
            try:
                existing = await self._dispatch.run_for_head(
                    repository, workflow, ref, commit_sha=commit_sha
                )
            except TokenPermissionError as exc:
                # Reading a ref's head needs Contents, and a PAT scoped to
                # Actions alone does not have it. Failing here with an
                # actionable message beats dispatching a possible duplicate.
                raise ValidationError(
                    "Cannot check whether this commit has run: reading the head of "
                    f"'{ref}' needs Contents read on the credential. Pass commit_sha "
                    "with the commit you care about, or use a token with Contents read."
                ) from exc
            if existing is not None:
                short_sha = (existing.commit_sha or "")[:7]
                return RunView(
                    run_id=existing.github_run_id,
                    status=existing.latest_status or "queued",
                    message=(
                        f"'{workflow}' already has a run for {ref}"
                        + (f" at {short_sha}" if short_sha else "")
                        + " on '"
                        + repository
                        + "'; nothing was dispatched."
                    ),
                    html_url=_run_url(repository, existing.github_run_id),
                    attempt=existing.latest_attempt,
                    dispatched=False,
                    duration_sample=await self._runs.duration_sample(existing.workflow_id),
                )

        run = await self._dispatch.dispatch(repository, workflow, ref, inputs=inputs)
        return RunView(
            run_id=run.github_run_id,
            status=run.latest_status or "queued",
            message=f"Workflow '{workflow}' triggered on '{repository}' ({ref}).",
            html_url=_run_url(repository, run.github_run_id),
            attempt=run.latest_attempt,
            # The caller's next move is a wait, and this is the only moment the
            # budget can be handed over without a second round trip. ``None``
            # on a fresh workflow: there is no history to sample, and inventing
            # a number would be worse than saying nothing.
            duration_sample=await self._runs.duration_sample(run.workflow_id),
        )

    async def rerun_workflow(self, repository: str, run_id: int) -> RunView:
        """Re-run a previous run.

        Args:
            repository: ``owner/repo``.
            run_id: GitHub run id of the run to re-run.

        Returns:
            A :class:`RunView` for the same run, now at its new attempt. The
            ``run_id`` is unchanged: GitHub reuses it and increments the attempt
            number.

        Raises:
            RunNotFoundError: The run is unknown locally or belongs to another
                repository.
            TriggerFailedError: GitHub refused the re-run.
        """
        run, previous = await self._dispatch.rerun(repository, run_id)
        attempt = run.latest_attempt or previous
        advanced = attempt > previous
        return RunView(
            run_id=run.github_run_id,
            status=run.latest_status or "queued",
            message=(
                f"Run {run_id} of '{repository}' re-started as attempt {attempt} "
                f"(was {previous}); the run id is unchanged."
                if advanced
                else (
                    f"Run {run_id} of '{repository}': GitHub accepted the re-run, "
                    f"but still reports attempt {attempt}. Read it again shortly; "
                    "do not re-run a second time."
                )
            ),
            html_url=_run_url(repository, run.github_run_id),
            attempt=attempt,
            previous_attempt=previous,
            duration_sample=await self._runs.duration_sample(run.workflow_id),
        )

    async def cancel_run(self, repository: str, run_id: int) -> RunView:
        """Cancel a queued or in-progress run.

        Args:
            repository: ``owner/repo``.
            run_id: GitHub run id to cancel.

        Returns:
            The run state GitHub reports right after the request. It may still be
            ``in_progress`` for a moment: cancellation is asynchronous there.

        Raises:
            RunNotFoundError: The run is unknown locally or belongs to another
                repository.
            TriggerFailedError: GitHub refused the cancellation.
        """
        run = await self._dispatch.cancel(repository, run_id)
        return RunView(
            run_id=run.github_run_id,
            status=run.latest_status or "queued",
            message=(
                f"Cancellation of run {run_id} of '{repository}' requested; "
                f"GitHub reports {run.latest_status}."
            ),
            html_url=_run_url(repository, run.github_run_id),
        )

    async def wait_for_completion(
        self,
        repository: str,
        run_id: int,
        timeout: float | None = 600.0,
        poll_interval: float = 5.0,
    ) -> RunWaitView:
        """Wait for a run to finish, with the pull channel at the edges.

        Three phases, and the shape is the point:

        1. One opening read adopts the run if this process has never seen it and
           refreshes the row, so the loop starts from a known state.
        2. The middle loop reads the run alone -- no jobs -- and stops the moment
           the row says completed, which a webhook may already have written.
        3. One closing read stores the jobs and steps, so the caller never pays
           for a fetch right after the wait.

        A run that is already finished costs exactly one read.

        Args:
            repository: ``owner/repo``.
            run_id: GitHub run id.
            timeout: Maximum seconds to wait; ``None`` waits forever.
            poll_interval: Seconds between completion checks.

        Returns:
            A :class:`RunWaitView`; ``completed`` is False when the timeout
            expired while the run was still in flight.

        Raises:
            RepositoryNotFoundError: The repository is not tracked.
            RunNotFoundError: The run does not exist on GitHub.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout is not None else None

        run = await self._runs.fetch_by_name(repository, run_id)
        while run.latest_status != "completed":
            if deadline is not None and loop.time() >= deadline:
                return _wait_view(
                    run,
                    completed=False,
                    message=f"Run {run_id} still '{run.latest_status}' after {timeout:.0f}s.",
                    sample=await self._runs.duration_sample(run.workflow_id),
                )
            await asyncio.sleep(poll_interval)
            run = await self._runs.fetch_by_name(repository, run_id, with_jobs=False)

        # The run is finished; read it once more so the jobs and steps of the
        # attempt that produced this state are stored too.
        run = await self._runs.fetch_by_name(repository, run_id)
        return _wait_view(
            run,
            completed=True,
            message=f"Run {run_id} finished with '{run.latest_conclusion or 'unknown'}'.",
        )


def _wait_view(
    run: AsyncWorkflowRun,
    *,
    completed: bool,
    message: str,
    sample: DurationSample | None = None,
) -> RunWaitView:
    """Build the wait result from a stored run."""
    return RunWaitView(
        run_id=run.github_run_id,
        status=run.latest_status or "",
        conclusion=run.latest_conclusion,
        message=message,
        completed=completed,
        duration_seconds=_elapsed_seconds(run.latest_started_at, run.latest_completed_at),
        duration_sample=sample,
    )


__all__ = ["TriggerController"]
