# src/rhosocial/github_ci_tracker/controllers/dispatch.py
"""Dispatch, re-run and cancel: the three verbs that spend CI minutes.

Nothing else in the system can do these: only this controller is typed against
:class:`~rhosocial.github_ci_tracker.services.github.protocols.RunControl`, and
only its constructor is handed one.

The flow is short on purpose. Resolve the repository and the workflow locally,
ask GitHub to start (or stop) the run, then hand whatever GitHub answered to
:class:`~rhosocial.github_ci_tracker.controllers.RunData`, which gives it to
the model -- the model is what knows a payload's fields, its state machine and
the attempt a job hangs off.

There is no authorization step. A dispatch happens because a person asked for
it; the credential's Actions-write scope is the limit, and GitHub enforces that
itself. The one local check is scope, not permission: a run that belongs to
another repository is refused here rather than by asking GitHub to touch it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..core.exceptions import (
    CitrackerError,
    RepositoryNotFoundError,
    RunNotFoundError,
    TriggerFailedError,
    map_api_error,
)
from ..services.github.protocols import RunControl, RunReader

if TYPE_CHECKING:  # the rows these flows return; only the annotation names them
    from ..core.run_tracker import RunTracker
    from ..models import AsyncRepository, AsyncWorkflow, AsyncWorkflowRun
    from .run import RunData

logger = logging.getLogger(__name__)

#: How many times the read-back looks for a run GitHub did not name, and how
#: long it waits between attempts. A dispatch that answered ``204`` has created
#: the run already; the list endpoint just needs a moment to include it.
RESOLVE_ATTEMPTS = 3
RESOLVE_DELAY = 1.0


def _as_int(value: Any) -> int | None:
    """Coerce a GitHub scalar to ``int``, returning ``None`` when impossible."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dispatch_identifier(workflow: AsyncWorkflow) -> str:
    """The identifier ``POST .../dispatches`` accepts for ``workflow``.

    A file name (``test.yml``) or the numeric id -- **not** the repository path.
    Passing ``.github/workflows/test.yml`` answers 404, which reads like "no such
    workflow" and sends the caller hunting for a permission problem that is not
    there.
    """
    path = (workflow.path or "").strip()
    if path:
        return path.rsplit("/", 1)[-1]
    return str(workflow.workflow_id)


def _describes_workflow(payload: Mapping[str, Any], workflow: AsyncWorkflow) -> bool:
    """Whether a run payload belongs to ``workflow``.

    The path is compared first because it is stable across renames, and it is
    the only identifier available when the local row has ``workflow_id = 0``.
    """
    path = str(payload.get("path") or "").strip()
    if workflow.path and path:
        return path == workflow.path
    name = str(payload.get("name") or "").strip().lower()
    return bool(workflow.name) and name == workflow.name.strip().lower()


class DispatchController:
    """Start runs, repeat one, stop one.

    Args:
        control: The write verbs. Typed against ``RunControl`` so a test can
            record calls without implementing the read half.
        readers: The read verbs, used to name the run GitHub created and to see
            a re-run's new attempt.
        runs_data: The controller that owns run storage.
        tracker: The tracker the webhook channel consults to decide whether a
            delivery belongs to a run this service started. ``None`` means "do
            not track", which a script that does not receive webhooks wants; a
            server passes the process-wide one.
    """

    def __init__(
        self,
        *,
        control: RunControl,
        readers: RunReader,
        runs_data: RunData,
        tracker: RunTracker | None = None,
    ) -> None:
        self._control = control
        self._readers = readers
        self._store = runs_data
        self._tracker = tracker

    # -- the verbs ------------------------------------------------------------

    async def dispatch(
        self,
        repo_full_name: str,
        workflow: str,
        ref: str,
        *,
        inputs: dict[str, str] | None = None,
        resolve_attempts: int = RESOLVE_ATTEMPTS,
        resolve_delay: float = RESOLVE_DELAY,
    ) -> AsyncWorkflowRun:
        """Dispatch a workflow on ``ref`` and store the run it creates.

        A dispatch is not idempotent. When the answer is unclear, the caller
        checks for an existing run with ``find_runs`` before calling again --
        this method cannot do that check for them, because "the same dispatch"
        is not something GitHub lets it recognise.

        Args:
            repo_full_name: ``owner/repo``.
            workflow: Workflow file name, path, display name or numeric id.
            ref: Branch or tag to run on.
            inputs: Optional ``workflow_dispatch`` inputs.
            resolve_attempts: Read-back attempts, used only when GitHub answers
                ``204`` without naming the run.
            resolve_delay: Seconds waited between read-back attempts.

        Returns:
            The stored run.

        Raises:
            RepositoryNotFoundError: The repository is not tracked.
            WorkflowNotFoundError: The workflow is not known locally; the
                message lists the workflows the repository does have.
            TriggerFailedError: GitHub refused the dispatch, or the run it
                created could not be resolved.
        """
        repo = await self._repository(repo_full_name)
        record = await self._store.workflow(repo, workflow)

        try:
            payload = await self._control.dispatch(
                repo.full_name,
                _dispatch_identifier(record),
                ref,
                inputs,
            )
        except CitrackerError as exc:
            raise map_api_error(exc) from exc

        if not _as_int(payload.get("run_id")):
            # GitHub answered 204: the run exists, but it was not named. The
            # read-back races with any concurrent dispatch, which is why the
            # answer is verified rather than trusted.
            found = await self._discover_run(
                repo,
                workflow=record,
                ref=ref,
                attempts=resolve_attempts,
                delay=resolve_delay,
            )
            if found is None:
                raise TriggerFailedError(
                    "GitHub accepted the workflow dispatch but the run could not be resolved",
                    status_code=202,
                )
            payload = found

        github_run_id = _as_int(payload.get("id") or payload.get("run_id")) or 0
        if not github_run_id:
            raise TriggerFailedError(
                "GitHub accepted the workflow dispatch but returned no run id",
                status_code=202,
            )

        return await self._adopt_triggered(repo, payload, github_run_id)

    async def run_for_head(
        self,
        repo_full_name: str,
        workflow: str,
        ref: str,
        *,
        commit_sha: str | None = None,
    ) -> AsyncWorkflowRun | None:
        """The run of ``workflow`` that already exists for a commit of ``ref``.

        A dispatch is not idempotent, so a caller that wants "make sure this
        commit has run" needs the question answered before spending a second run.
        The runs of that workflow on that sha are read from GitHub -- not from the
        local cache, because a run someone else started is exactly the one the
        cache may not have.

        Args:
            commit_sha: The commit to check. Read from the ref's head when
                omitted, which needs Contents read on the credential; a
                fine-grained token scoped to Actions alone is refused by that
                endpoint, and the caller is then asked to pass the sha -- they
                usually have it, since a dispatch is asked for after a push.

        Returns:
            The stored run, adopting it when GitHub knows one the database does
            not. ``None`` when that commit has not been run by this workflow.
        """
        repo = await self._repository(repo_full_name)
        record = await self._store.workflow(repo, workflow)
        head_sha = commit_sha or await self._readers.get_ref_sha(repo.full_name, ref)
        if not head_sha:
            return None

        try:
            payloads = await self._readers.list_runs(
                repo.full_name,
                workflow_id=record.workflow_id or None,
                branch=ref,
                head_sha=head_sha,
                per_page=20,
            )
        except CitrackerError as exc:
            raise map_api_error(exc) from exc

        for payload in payloads:
            if str(payload.get("head_sha") or "") not in ("", head_sha):
                continue
            if not _describes_workflow(payload, record):
                continue
            github_run_id = _as_int(payload.get("id")) or 0
            if not github_run_id:
                continue
            stored = await self._store.run(github_run_id)
            return stored if stored is not None else await self._store.adopt(repo, payload)
        return None

    async def rerun(
        self,
        repo_full_name: str,
        run_id: int,
        *,
        resolve_attempts: int = RESOLVE_ATTEMPTS,
        resolve_delay: float = RESOLVE_DELAY,
    ) -> tuple[AsyncWorkflowRun, int]:
        """Repeat a finished run.

        **No new run is created.** GitHub reuses the id and increments
        ``run_attempt``, so the stored record is the same one with its new
        attempt added; the caller's ``run_id`` stays valid.

        The new attempt is read back, not assumed. GitHub accepts the request and
        its own read can lag by a moment; reporting the attempt the run was
        already at would tell the caller their re-run did not happen, and leave
        them to verify it by hand.

        Args:
            repo_full_name: ``owner/repo``.
            run_id: GitHub run id of the run to repeat.
            resolve_attempts: Reads spent waiting for ``run_attempt`` to advance.
            resolve_delay: Seconds waited between those reads.

        Returns:
            ``(run, previous_attempt)``: the stored run at whatever attempt
            GitHub reports now, and the attempt it was at before the request.
            The two are equal when GitHub had not caught up yet -- the caller is
            told that rather than shown a number it never said.

        Raises:
            RepositoryNotFoundError: The repository is not tracked.
            RunNotFoundError: The run is unknown locally or belongs to another
                repository.
            RunInProgressError: The run is still running, so GitHub refused.
            TriggerFailedError: GitHub refused the re-run for another reason, or
                the run could not be read back afterwards.
        """
        repo = await self._repository(repo_full_name)
        run = await self._run(repo, run_id)
        previous = run.latest_attempt or 0

        try:
            await self._control.rerun(repo.full_name, run_id)
        except CitrackerError as exc:
            raise map_api_error(exc) from exc

        try:
            payload = await self._read_rerun(
                repo.full_name,
                run_id,
                previous,
                attempts=resolve_attempts,
                delay=resolve_delay,
            )
        except CitrackerError as exc:
            raise TriggerFailedError(
                f"GitHub accepted the re-run of run {run_id}, "
                f"but the run could not be read back: {exc}",
                status_code=202,
            ) from exc

        updated = await self._store.update(run, payload)
        self._track(run_id)
        logger.info(
            "Re-ran run %s of '%s' as attempt %s (was %s)",
            run_id,
            repo.full_name,
            updated.latest_attempt,
            previous,
        )
        return updated, previous

    async def _read_rerun(
        self,
        repo_full_name: str,
        run_id: int,
        previous: int,
        *,
        attempts: int,
        delay: float,
    ) -> Mapping[str, Any]:
        """Read the run back, giving GitHub a moment to advance the attempt.

        Bounded on purpose: the re-run was accepted, so a lag must not become an
        error. After the last read the payload is returned as-is, and the caller
        can see that the attempt did not move.
        """
        payload: Mapping[str, Any] = {}
        for index in range(max(1, attempts)):
            payload = await self._readers.get_run(repo_full_name, run_id)
            if (_as_int(payload.get("run_attempt")) or 0) > previous:
                return payload
            if index + 1 < attempts:
                await asyncio.sleep(delay)

        logger.warning(
            "Re-run of run %s was accepted, but GitHub still reports attempt %s (was %s)",
            run_id,
            _as_int(payload.get("run_attempt")),
            previous,
        )
        return payload

    async def cancel(self, repo_full_name: str, run_id: int) -> AsyncWorkflowRun:
        """Cancel a queued or in-progress run.

        GitHub acknowledges before the runner stops, so the record is re-read
        rather than assumed: the caller is told what GitHub says now, which may
        still be ``in_progress``. A failed read-back is not an error -- the
        cancellation was accepted, and the push channel will bring the state in.

        Args:
            repo_full_name: ``owner/repo``.
            run_id: GitHub run id to cancel.

        Returns:
            The stored run, at whatever state GitHub last reported.

        Raises:
            RepositoryNotFoundError: The repository is not tracked.
            RunNotFoundError: The run is unknown locally or belongs to another
                repository.
            TriggerFailedError: GitHub refused the cancellation.
        """
        repo = await self._repository(repo_full_name)
        run = await self._run(repo, run_id)

        try:
            await self._control.cancel(repo.full_name, run_id)
        except CitrackerError as exc:
            raise map_api_error(exc) from exc

        try:
            payload = await self._readers.get_run(repo.full_name, run_id)
        except CitrackerError:
            return run

        cancelled = await self._store.update(run, payload)
        logger.info("Cancelled run %s of '%s'", run_id, repo.full_name)
        return cancelled

    # -- the internals --------------------------------------------------------

    async def _repository(self, repo_full_name: str) -> AsyncRepository:
        """The tracked repository, or ``RepositoryNotFoundError``."""
        repo = await self._store.repository(repo_full_name)
        if repo is None:
            raise RepositoryNotFoundError(f"Repository '{repo_full_name}' is not tracked")
        return repo

    async def _run(self, repo: AsyncRepository, run_id: int) -> AsyncWorkflowRun:
        """The stored run, provided it belongs to ``repo``."""
        run = await self._store.run(run_id)
        if run is None:
            raise RunNotFoundError(f"Run {run_id} not found")

        workflow = await self._store.workflow_of(run)
        if workflow is None or workflow.repository_id != repo.id:
            raise RunNotFoundError(f"Run {run_id} does not belong to repository '{repo.full_name}'")
        return run

    async def _adopt_triggered(
        self, repo: AsyncRepository, payload: Mapping[str, Any], github_run_id: int
    ) -> AsyncWorkflowRun:
        """Store the run GitHub created, then note it as ours.

        The tracker entry is what lets a webhook delivery be attributed to this
        process's dispatch rather than to an unrelated run.
        """
        run = await self._store.run(github_run_id)
        if run is None:
            if not payload.get("status") or not payload.get("conclusion"):
                # A dispatch response carries ids only; read the run so the
                # stored row has a real status rather than a guess.
                try:
                    payload = await self._readers.get_run(repo.full_name, github_run_id)
                except CitrackerError:
                    pass
            run = await self._store.adopt(repo, payload)
        self._track(github_run_id)
        return run

    def _track(self, github_run_id: int) -> None:
        """Note a run this service started, when a tracker was given."""
        if self._tracker is not None:
            self._tracker.add(github_run_id)

    async def _discover_run(
        self,
        repo: AsyncRepository,
        *,
        workflow: AsyncWorkflow,
        ref: str,
        attempts: int,
        delay: float,
    ) -> Mapping[str, Any] | None:
        """Find the run GitHub created for an accepted dispatch.

        ``POST .../dispatches`` may answer with no body, so the run id is read
        back from the runs list: the newest run of that workflow on that ref that
        this database has not stored yet is the one just created.

        Returns:
            The run payload, or ``None`` when nothing new appeared in time.
        """
        for attempt in range(max(1, attempts)):
            try:
                runs = await self._readers.list_runs(
                    repo.full_name,
                    workflow_id=workflow.workflow_id or None,
                    branch=ref,
                )
            except CitrackerError as exc:
                raise map_api_error(exc) from exc

            for candidate in runs:
                github_run_id = _as_int(candidate.get("id"))
                if not github_run_id:
                    continue
                if await self._store.run(github_run_id) is None:
                    return candidate

            if attempt + 1 < max(1, attempts):
                await asyncio.sleep(delay)
        return None


__all__ = ["DispatchController"]
