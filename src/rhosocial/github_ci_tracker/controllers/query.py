# src/rhosocial/github_ci_tracker/controllers/query.py
"""Answer the questions a caller asks about runs, jobs and logs.

The cache exists to be *read*, never to be believed: a run's state is whatever
GitHub says it is, so every question about a run goes to the source and the
answer is written back when it differs. The push channel (webhooks) only makes
that answer cheaper to reach -- it can be late, dropped, or never armed at all,
and a run can be created by someone else and asked about here for the first time.

Sub-resources are the exception, by design: attempts, jobs, steps and logs are
append-only history, and fetching them costs one call per level. They are
updated when asked for, and a finished execution's rows are terminal, so once
stored they are served without another call.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ..core.exceptions import (
    AttemptNotFoundError,
    CitrackerError,
    GithubAPIError,
    JobNotFoundError,
    RunNotFoundError,
    WorkflowNotFoundError,
    map_api_error,
)
from ..models.states import FAILED_CONCLUSIONS
from ..services.github.protocols import RepositoryReader, RunReader
from ..views import (
    JobFailure,
    JobHistoryView,
    JobLogsView,
    JobRecord,
    LogCaptureView,
    RepositoryRuns,
    RunRecord,
    RunRefreshView,
    StepSection,
    _cache_state,
    _run_url,
    _to_job_record,
    _to_run_record,
    as_utc,
)
from .logs import RunLogCapture
from .run import RunController

if TYPE_CHECKING:  # the rows these flows return; only the annotation names them
    from ..models import AsyncJob, AsyncRepository, AsyncStep, AsyncWorkflowRun
    from ..models.attempt import AsyncRunAttempt
    from .run import RunData

logger = logging.getLogger(__name__)

#: Default and maximum page size for the list endpoints.
DEFAULT_PER_PAGE = 30
MAX_PER_PAGE = 100


def _line_count(content: str) -> int:
    """Lines in ``content``, counted the way ``search_log`` counts them.

    ``splitlines`` drops a trailing newline's empty tail, which is the number a
    caller sees when the same text comes back from a search. Counting ``\n`` and
    adding one for a missing terminator disagreed with it by one on the files
    GitHub writes (every line ends with a newline).
    """
    return len(content.splitlines())


def _missing_reason(report: Any, step: int) -> str:
    """Why one asked-for step has no text.

    The capture's own verdict wins: "the archive was gone by the time we looked"
    and "we were told not to capture" lead a caller to different next steps,
    and a generic "missing" hides both.
    """
    if report is not None and report.skipped_reason:
        return str(report.skipped_reason)
    return "step_not_captured"


def _failed_reason(report: Any, steps: Sequence[Any]) -> str:
    """Why a failed-steps-only read came back empty."""
    failed = [item for item in steps if (item.conclusion or "").lower() in FAILED_CONCLUSIONS]
    if not failed:
        return "no_failed_steps"
    if report is not None and report.skipped_reason:
        return str(report.skipped_reason)
    return "step_logs_not_captured"


class QueryController:
    """Query runs, jobs and steps, and read job logs.

    Args:
        readers: The GitHub read verbs. Every run-level answer comes from here.
        repositories: Repository metadata and workflow lists.
        runs: The read path that adopts or updates a run.
        runs_data: The controller that owns run storage.
    """

    def __init__(
        self,
        *,
        readers: RunReader,
        repositories: RepositoryReader,
        runs: RunController,
        runs_data: RunData,
        logs: RunLogCapture | None = None,
    ) -> None:
        self._readers = readers
        self._repositories = repositories
        self._runs = runs
        self._store = runs_data
        #: Step-log capture and reads. ``None`` means "this deployment has no
        #: step logs": every log read then answers from the merged job document,
        #: which is exactly the behaviour before step logs existed.
        self._logs = logs

    # -- one run ---------------------------------------------------------------

    async def get_run_status(
        self, repository: str, run_id: int, attempt: int | None = None
    ) -> RunRecord:
        """Read one run -- or one of its attempts -- from GitHub and store it.

        There is no cache short-circuit. A finished run is not "settled enough
        to skip": it can be re-run, and a webhook may never have arrived, so the
        only answer this service is willing to give is GitHub's.

        ``attempt`` selects one execution of the run. Omitted, the newest attempt
        answers, which is GitHub's own default for a run id. Given, that attempt
        is read from GitHub too, compared with the stored row and written back
        when the two disagree -- so a row left behind by an older build, or by a
        delivery that never arrived, is corrected the next time someone asks for
        it. Reading a past attempt never touches the run's current state.

        Args:
            repository: ``owner/repo``.
            run_id: GitHub run id.
            attempt: Attempt number to report; ``None`` for the newest.

        Raises:
            RepositoryNotFoundError: The repository is not tracked.
            RunNotFoundError: The run belongs to another repository.
            AttemptNotFoundError: The run has no attempt with that number.
            GithubAPIError: GitHub refused the read; nothing is answered from
                the cache in that case.
        """
        repo = await self._runs.repository(repository)
        if attempt is None:
            run = await self._runs.fetch(repo, run_id, with_jobs=False)
            record = await self._record(run, repository)
            await self._fill_base_ref(repository, record, run)
            return record

        # The run's derived state is needed even though this call answers for an
        # attempt: ``latest_attempt`` is what says whether this attempt is the
        # newest, and the cache facts are computed against it.
        run = await self._store.run(run_id, with_state=True)
        if run is None:
            # An attempt needs a run to hang off, and the run's own state has to
            # be known to tell the caller whether this attempt is the newest.
            run = await self._runs.fetch(repo, run_id, with_jobs=False)
        else:
            await self._require_scope(run, repo)

        try:
            payload = await self._readers.get_run_attempt(repository, run_id, attempt)
        except CitrackerError as exc:
            mapped = map_api_error(exc)
            if isinstance(mapped, GithubAPIError) and mapped.status_code == 404:
                raise AttemptNotFoundError(
                    f"Run {run_id} has no attempt {attempt}; GitHub reports "
                    f"{run.latest_attempt or 0} attempt(s)."
                ) from exc
            raise mapped from exc

        stored = await self._store.store_attempt(run, payload)
        record = await self._attempt_record(run, repository, stored)
        await self._fill_base_ref(repository, record, run)
        return record

    async def get_run_status_by_id(self, run_id: int) -> RunRecord:
        """Read one run from GitHub, resolving its repository locally first.

        GitHub has no "run by id" endpoint without a repository, so the local
        row is used only to learn *which* repository to ask about; the state
        itself still comes from GitHub.

        Args:
            run_id: GitHub run id.

        Raises:
            RunNotFoundError: This service has never stored the run, so its
                repository is unknown.
        """
        stored = await self._store.run(run_id, with_state=False)
        if stored is None:
            raise RunNotFoundError(
                f"Run {run_id} is not stored locally, so its repository is unknown. "
                "Pass repository='owner/repo' to read it from GitHub."
            )
        repository = await self._repository_name(stored)
        return await self.get_run_status(repository, run_id)

    # -- lists -----------------------------------------------------------------

    async def list_runs(
        self,
        repository: str,
        workflow: str | None = None,
        branch: str | None = None,
        status: str | None = None,
        commit_sha: str | None = None,
        per_page: int = 20,
        page: int = 1,
    ) -> list[RunRecord]:
        """Read a page of runs from GitHub, store it and answer from the rows.

        GitHub filters what it can (``status``, ``branch``, a full ``head_sha``);
        a conclusion or an abbreviated sha is filtered here, over the page that
        was read, because the endpoint cannot express those.

        Args:
            repository: ``owner/repo``.
            workflow: Workflow file name, path, display name or numeric id.
            branch: Only runs of this branch.
            status: A run status or a conclusion.
            commit_sha: Full or abbreviated head sha.
            per_page: Page size, clamped to ``MAX_PER_PAGE``.
            page: 1-based page number.

        Raises:
            RepositoryNotFoundError: The repository is not tracked.
            WorkflowNotFoundError: The workflow reference resolves nowhere.
            GithubAPIError: GitHub refused the read.
        """
        repo = await self._runs.repository(repository)
        github_workflow_id = await self._github_workflow_id(repo, workflow)

        state = (status or "").strip().lower()
        github_status = state if state in {"queued", "in_progress", "completed"} else None
        conclusion = state if state and github_status is None else None

        sha = (commit_sha or "").strip().lower()
        full_sha = sha if len(sha) >= 40 else None

        try:
            payloads = await self._readers.list_runs(
                repository,
                workflow_id=github_workflow_id,
                status=github_status,
                branch=branch,
                head_sha=full_sha,
                per_page=_clamp(per_page),
                page=max(page, 1),
            )
        except CitrackerError as exc:
            raise map_api_error(exc) from exc

        runs = await self._upsert_page(repo, payloads)
        if conclusion is not None:
            runs = [run for run in runs if (run.latest_conclusion or "") == conclusion]
        if sha and full_sha is None:
            runs = [run for run in runs if (run.commit_sha or "").startswith(sha)]

        # A list is a summary: jobs and steps belong to ``get_run``, and loading
        # them here cost three queries plus every step row per page.
        return await self.records(runs, repository, with_jobs=False)

    async def list_run_summaries(
        self,
        repositories: Sequence[str],
        *,
        workflow: str | None = None,
        branch: str | None = None,
        per_page: int = 3,
    ) -> list[RepositoryRuns]:
        """Newest runs of several repositories, read from GitHub per repository.

        One list call per repository -- the price of a truthful answer, since a
        webhook may never have reported the run at all. A repository that cannot
        be reached carries the error inside its entry instead of aborting the
        batch: a caller asking about fifteen repositories must still learn about
        the fourteen that answered.

        Args:
            repositories: ``owner/repo`` names, in answer order.
            workflow: Optional workflow reference, resolved per repository.
            branch: Only runs of this branch.
            per_page: Runs per repository.
        """
        names = list(dict.fromkeys(name for name in repositories if name))
        results = {name: RepositoryRuns(repository=name) for name in names}
        if not names:
            return []

        for name in names:
            try:
                records = await self.list_runs(
                    name,
                    workflow=workflow,
                    branch=branch,
                    per_page=per_page,
                    page=1,
                )
            except CitrackerError as exc:
                results[name].error = exc
                continue
            results[name].runs = records

        await self.attach_failed_jobs(
            [record for result in results.values() for record in result.runs]
        )
        return [results[name] for name in names]

    async def get_latest_run(
        self,
        repository: str,
        branch: str | None = None,
        workflow: str | None = None,
    ) -> RunRecord | None:
        """Read the newest matching run from GitHub.

        The newest run of the **whole repository**, unless ``branch`` or
        ``workflow`` narrows it. There is deliberately no implicit "default
        branch" filter: an answer that silently ignores a branch the caller never
        mentioned reads as authoritative while being about something else --
        "latest run" once answered with a month-old run on ``main`` while the
        branch under work had run minutes earlier.

        Args:
            repository: ``owner/repo``.
            branch: Branch to look at; omit for any branch.
            workflow: Optional workflow file name, path or numeric id.

        Returns:
            The newest matching run, or ``None`` when GitHub reports none.
        """
        repo = await self._runs.repository(repository)
        github_workflow_id = await self._github_workflow_id(repo, workflow)
        try:
            payloads = await self._readers.list_runs(
                repository,
                workflow_id=github_workflow_id,
                branch=branch,
                per_page=1,
            )
        except CitrackerError as exc:
            raise map_api_error(exc) from exc

        if not payloads:
            return None
        run = await self._upsert(repo, payloads[0])
        return await self._record(run, repository)

    # -- the pull channel ------------------------------------------------------

    async def refresh_run(self, repository: str, run_id: int) -> RunRefreshView:
        """Force a full sync of one run, jobs included.

        The pull channel is now also the default read path, so this is the
        explicit form: same read, plus the job tree and a summary of what
        changed.
        """
        repo = await self._runs.repository(repository)
        try:
            remote = await self._readers.get_run(repository, run_id)
        except CitrackerError as exc:
            mapped = map_api_error(exc)
            if isinstance(mapped, GithubAPIError) and mapped.status_code == 404:
                raise RunNotFoundError(f"Run {run_id} not found on GitHub") from exc
            raise mapped from exc

        stored = await self._store.run(run_id, with_state=False)
        created = stored is None
        previous_status = None if stored is None else stored.latest_status

        if stored is None:
            run = await self._store.adopt(repo, remote)
        else:
            await self._require_scope(stored, repo)
            run = await self._store.update(stored, remote)

        jobs = await self._readers.get_run_jobs(repository, run_id)
        job_ids = await self._store.sync_jobs(run, jobs)

        return RunRefreshView(
            run_id=run.github_run_id,
            run_number=run.run_number,
            previous_status=previous_status,
            status=run.latest_status or "queued",
            conclusion=run.latest_conclusion,
            created=created,
            changed=created or previous_status != run.latest_status,
            jobs_synced=len(job_ids),
            steps_synced=await self._store.step_count(job_ids),
            html_url=_run_url(repository, run.github_run_id),
        )

    # -- history and identity --------------------------------------------------

    async def job_failure_history(
        self, repository: str, job_name: str, sample: int = 10
    ) -> JobHistoryView | None:
        """How often a job name failed across this repository's recent executions.

        Answers "should I re-run, or fix the code?" from the stored rows only:
        the runs in question are already here, and a diagnostic question must
        not spend GitHub quota.

        One execution is one attempt, so a run re-run three times counts three
        times -- those are three separate tries, which is the unit the caller is
        deciding about.

        Args:
            repository: ``owner/repo``.
            job_name: The job's display name, as GitHub reports it.
            sample: How many of the newest stored runs to look at.

        Returns:
            The history, or ``None`` when there is nothing to sample -- an
            untracked repository, no runs, no attempts, or no job by that name.
        """
        repo = await self._store.repository(repository)
        if repo is None:
            return None

        workflows = await self._store.workflows(repo)
        workflow_ids = [workflow.id for workflow in workflows if workflow.id is not None]
        if not workflow_ids:
            return None

        run_ids = await self._store.recent_run_ids(workflow_ids, limit=sample)
        if not run_ids:
            return None
        attempt_ids = await self._store.attempt_ids(run_ids)
        if not attempt_ids:
            return None

        conclusions = await self._store.job_conclusions(attempt_ids, job_name)
        if not conclusions:
            return None
        return JobHistoryView(
            job_name=job_name,
            sampled_executions=len(conclusions),
            failures=sum(1 for conclusion in conclusions if conclusion == "failure"),
        )

    async def repository_name_of_job(self, job_id: int) -> str:
        """The ``owner/repo`` a stored job belongs to.

        Job ids are globally unique on GitHub, so a caller holding one should not
        have to remember the repository. The walk is job -> attempt -> run ->
        workflow -> repository, over stored rows.

        Raises:
            JobNotFoundError: This service has never stored the job.
            RunNotFoundError: The job's run or repository row is gone.
        """
        job = await self._store.job(job_id)
        if job is None:
            raise JobNotFoundError(f"Job {job_id} not found")
        run = await self._store.run_of(job)
        repo = None if run is None else await self._store.repository_of(run)
        if repo is None:
            raise RunNotFoundError(f"Repository of job {job_id} not found")
        return repo.full_name

    async def repository_name_of_run(self, run_id: int) -> str:
        """The ``owner/repo`` a stored run belongs to.

        GitHub has no run-by-id endpoint, so a caller holding only a run id has
        to learn the repository from this service's rows; the run's *state* still
        comes from GitHub when it is read.

        Raises:
            RunNotFoundError: This service has never stored the run, which is
                also the only case where the caller must name the repository.
        """
        run = await self._store.run(run_id, with_state=False)
        repo = None if run is None else await self._store.repository_of(run)
        if repo is None:
            raise RunNotFoundError(
                f"Run {run_id} is not tracked locally, so its repository cannot be "
                "resolved. Pass repository='owner/repo', or call find_runs to locate it."
            )
        return repo.full_name

    # -- jobs, steps, logs -----------------------------------------------------

    async def get_run_jobs(
        self, repository: str, run_id: int, *, with_steps: bool = True
    ) -> list[JobRecord]:
        """List the jobs of a run's current attempt.

        A finished run whose jobs are stored is answered from the rows: the
        execution is over, so nothing about it can change. Otherwise -- running,
        or no jobs stored yet -- the jobs are read from GitHub and stored.

        Args:
            with_steps: Load each job's steps. ``False`` answers the run-level
                question ("which job failed") without paying one step query per
                job -- a 50-job matrix would cost 50 otherwise. Step detail is
                :meth:`get_job_detail`'s job.
        """
        repo = await self._runs.repository(repository)
        run = await self._store.run(run_id, with_state=True)
        if run is None:
            raise RunNotFoundError(f"Run {run_id} not found")
        await self._require_scope(run, repo)

        jobs = await self._store.jobs(run)
        if not jobs or run.latest_status != "completed":
            remote_jobs = await self._readers.get_run_jobs(repository, run_id)
            await self._store.sync_jobs(run, remote_jobs)
            jobs = await self._store.jobs(run)

        if not with_steps:
            return [_to_job_record(job, run) for job in jobs]
        return [_to_job_record(job, run, await self._store.steps(job)) for job in jobs]

    async def get_job_detail(self, repository: str, job_id: int) -> JobRecord:
        """Get one job with its steps.

        Raises:
            RepositoryNotFoundError: The repository is not tracked.
            JobNotFoundError: The job is not in the database.
            RunNotFoundError: The job's run belongs to another repository.
        """
        repo = await self._runs.repository(repository)
        job = await self._store.job(job_id)
        if job is None:
            raise JobNotFoundError(f"Job {job_id} not found")

        run = await self._store.run_of(job)
        if run is None:
            raise RunNotFoundError(f"Run of job {job_id} not found")
        await self._require_scope(run, repo)

        steps = await self._store.steps(job)
        if job.is_finished and any(step.status != "completed" for step in steps):
            # The same inconsistency the webhook heals, seen from a read: a
            # finished job cannot have a step still running. One job read fixes
            # it, and the next read finds a consistent row.
            try:
                remote = await self._readers.get_job(repository, job_id)
                # Same reason as the webhook: the attempt is resolved from the
                # run's latest attempt, and ``run_of`` returns a row without the
                # derived fields.
                fresh = await self._store.run(run.github_run_id, with_state=True)
                await self._store.sync_jobs(fresh or run, [remote])
                steps = await self._store.steps(job)
            except CitrackerError as exc:
                logger.warning(
                    "Could not repair the steps of job %s: %s: %s",
                    job_id,
                    type(exc).__name__,
                    exc,
                )
        sizes = await self._store.step_log_sizes([step.id or 0 for step in steps])
        record = _to_job_record(job, run, steps, log_sizes=sizes)
        if self._logs is not None:
            record.capture = await self._logs.status(job)
        return record

    async def get_job_logs(
        self,
        repository: str,
        job_id: int,
        tail: int | None = None,
        *,
        step: int | None = None,
        failed_steps_only: bool = False,
    ) -> JobLogsView:
        """Get the logs of a job.

        Two sources, and the caller chooses by asking for a step:

        * no ``step`` and no ``failed_steps_only`` -- the merged document, from
          the stored job log or GitHub's single-job endpoint. **Unchanged** from
          before step logs existed: same rows, same refresh rule, same bytes.
        * ``step=N`` or ``failed_steps_only`` -- the captured step rows. A step
          whose file was never captured is reported as missing, never silently
          answered with another step's text. The capture had to happen while
          GitHub still split the run into per-step files (about an hour); that
          hour is a fetching deadline, not a retention limit: rows captured
          before it stay readable for good.

        A finished job's log is terminal, so the stored row is the answer. A
        running job's log is still growing: serving the stored copy would be
        serving a truncated document, so it is re-read every time until the job
        finishes.

        Args:
            repository: ``owner/repo``.
            job_id: GitHub job id.
            tail: Optional number of trailing lines to return (merged source only).
            step: Return only this step's captured text.
            failed_steps_only: Return the captured text of every step that did
                not pass.

        Raises:
            RepositoryNotFoundError: The repository is not tracked.
            JobNotFoundError: The job is not stored locally.
            RunNotFoundError: The job's run belongs to another repository.
        """
        repo = await self._runs.repository(repository)  # untracked is refused here
        job = await self._store.job(job_id)
        if job is None:
            raise JobNotFoundError(f"Job {job_id} not found")

        if step is not None or failed_steps_only:
            return await self._step_logs(repo, job, step=step, failed_steps_only=failed_steps_only)

        content = await self._store.log(job)
        if content is None or not job.is_finished:
            try:
                content = await self._readers.get_job_logs(repository, job_id)
            except CitrackerError as exc:
                if content is None:
                    raise map_api_error(exc) from exc
                # The stored copy is what we have; a failed refresh must not
                # turn an answer into an error.
                logger.warning("Could not refresh the log of job %s: %s", job_id, exc)
            else:
                await self._store.store_log(job, content)

        return JobLogsView(job_id=job_id, job_name=job.name, log_content=_tail(content, tail))

    async def refresh_step_logs(self, repository: str, job_id: int) -> LogCaptureView:
        """Capture this job's step logs again, retrying while the archive fills.

        The explicit form of what a step read does implicitly: useful after a
        capture ran out of retries. It can wait (six attempts, a minute apart by
        default), so it is a flag on ``get_job`` rather than something every read
        pays for. Past the window there is nothing to wait for -- the status says
        ``closed`` and this answers with that reason instead of sleeping.

        Raises:
            RepositoryNotFoundError: The repository is not tracked.
            JobNotFoundError: The job is not stored locally.
            RunNotFoundError: The job's run belongs to another repository.
        """
        repo = await self._runs.repository(repository)
        job = await self._store.job(job_id)
        if job is None:
            raise JobNotFoundError(f"Job {job_id} not found")
        run = await self._store.run_of(job)
        if run is None:
            raise RunNotFoundError(f"Run of job {job_id} not found")
        await self._require_scope(run, repo)
        if self._logs is None:
            return LogCaptureView(run_id=run.github_run_id, skipped_reason="capture_disabled")
        return await self._logs.ensure(repo, run, job=job, retry=True)

    async def _step_logs(
        self,
        repo: AsyncRepository,
        job: AsyncJob,
        *,
        step: int | None,
        failed_steps_only: bool,
    ) -> JobLogsView:
        """Answer a step-scoped log request from the captured rows.

        A capture is attempted first when the requested rows are missing and the
        run is still inside the window: the caller asking for a step is exactly
        the moment "was it captured" can still be answered with "yes, now it
        is". The capture waits for *these* steps only, and retries while GitHub
        has not written their files yet.
        """
        run = await self._store.run_of(job)
        if run is None:
            raise RunNotFoundError(f"Run of job {job.id} not found")
        await self._require_scope(run, repo)

        steps = await self._store.steps(job)
        wanted: set[int] | None = None
        if step is not None:
            wanted = {step}
        elif failed_steps_only:
            wanted = {
                item.number
                for item in steps
                if (item.conclusion or "").lower() in FAILED_CONCLUSIONS
            }

        report = None
        if self._logs is not None:
            # Scoped to the job: its own completion time decides the window, so a
            # finished job of a still-running matrix is capturable today. The
            # read path is the one place a retry is affordable -- a delivery
            # must answer quickly.
            report = await self._logs.ensure(repo, run, job=job, want=wanted, retry=True)
        texts = await self._store.step_log_texts(steps)

        missing = [item.number for item in steps if item.number not in texts]
        chosen = [item for item in steps if item.number in texts]
        if failed_steps_only:
            chosen = [
                item for item in chosen if (item.conclusion or "").lower() in FAILED_CONCLUSIONS
            ]

        # Even an empty answer is a step-scoped one: the caller asked for a step
        # and must not be handed the merged document as if it were that step.
        view = JobLogsView(job_id=job.github_job_id, job_name=job.name, source="step")
        if step is not None:
            selected = [item for item in chosen if item.number == step]
            if not selected:
                view.steps_without_logs = [step]
                view.reason = _missing_reason(report, step)
                return view
            view.sections = [self._section(selected[0], texts[selected[0].number])]
        else:
            view.sections = [self._section(item, texts[item.number]) for item in chosen]
            if not view.sections:
                view.reason = _failed_reason(report, steps)

        view.steps_without_logs = missing
        view.log_content = "\n".join(section.content.rstrip("\n") for section in view.sections)
        return view

    @staticmethod
    def _section(step: AsyncStep, content: str) -> StepSection:
        """One stored step log as a section.

        The BOM is stripped here, not at storage: the row keeps exactly what
        GitHub served, and the merged path already strips it before handing text
        out. A caller comparing the two must not see one start with ``\ufeff``.
        """
        text = content.lstrip("\ufeff")
        return StepSection(
            number=step.number,
            name=step.name,
            conclusion=step.conclusion,
            content=text,
            line_count=_line_count(text),
        )

    # -- internals -------------------------------------------------------------

    async def _require_scope(self, run: AsyncWorkflowRun, repo: AsyncRepository) -> None:
        """Refuse a run that belongs to another repository."""
        if not await self._store.belongs_to(run, repo):
            raise RunNotFoundError(
                f"Run {run.github_run_id} does not belong to repository '{repo.full_name}'"
            )

    async def _upsert(self, repo: AsyncRepository, payload: Mapping[str, Any]) -> AsyncWorkflowRun:
        """Store one run payload and return the row, newest-attempt state included."""
        github_run_id = int(payload.get("id") or 0)
        stored = await self._store.run(github_run_id, with_state=False) if github_run_id else None
        if stored is None:
            return await self._store.adopt(repo, payload)
        await self._require_scope(stored, repo)
        return await self._store.update(stored, payload)

    async def _upsert_page(
        self, repo: AsyncRepository, payloads: Sequence[Mapping[str, Any]]
    ) -> list[AsyncWorkflowRun]:
        """Store a page of run payloads, preserving GitHub's order."""
        runs: list[AsyncWorkflowRun] = []
        for payload in payloads:
            if not int(payload.get("id") or 0):
                continue
            runs.append(await self._upsert(repo, payload))
        return runs

    async def _github_workflow_id(self, repo: AsyncRepository, workflow: str | None) -> int | None:
        """The workflow id GitHub's runs endpoint accepts, or ``None`` for all."""
        if not workflow:
            return None

        # A cached row with ``workflow_id = 0`` is "not resolved yet", not "all
        # workflows": returning ``None`` there sent the query without a filter,
        # so ``recent_runs(workflow=...)`` answered with every workflow's runs
        # and said nothing. Fall through to GitHub instead, and remember the id.
        try:
            local = await self._store.workflow(repo, workflow)
        except WorkflowNotFoundError:
            local = None
        if local is not None and local.workflow_id:
            return local.workflow_id

        try:
            remote = await self._repositories.get_repo_workflows(repo.full_name)
        except CitrackerError as exc:
            raise map_api_error(exc) from exc

        wanted = workflow.strip()
        for item in remote:
            path = str(item.get("path") or "")
            matched = int(item.get("id") or 0) or None
            if not matched:
                continue
            if path and (path == wanted or path.endswith(f"/{wanted}") or path.endswith(wanted)):
                if local is not None:
                    await self._store.remember_workflow_id(local, matched)
                return matched

        # Paths and ids only, for the same reason the stored-row message uses
        # them: a display name is per branch, so listing one here would hand the
        # caller a reference that may not resolve tomorrow.
        available = ", ".join(
            sorted(f"{str(item.get('path') or '')} (id {item.get('id')})" for item in remote)
        )
        raise WorkflowNotFoundError(
            f"Workflow '{workflow}' not found in repository {repo.full_name}. "
            f"Use a path, a file name or the numeric workflow id: "
            f"{available or 'none are visible to this credential'}"
        )

    async def _repository_name(self, run: AsyncWorkflowRun) -> str:
        """The ``owner/repo`` a stored run belongs to, or ``""``."""
        repo = await self._store.repository_of(run)
        return "" if repo is None else repo.full_name

    async def _record(self, run: AsyncWorkflowRun, repo_full_name: str) -> RunRecord:
        """Build one run's view, jobs and cache facts included."""
        return (await self.records([run], repo_full_name))[0]

    async def _fill_base_ref(
        self, repository: str, record: RunRecord, run: AsyncWorkflowRun
    ) -> None:
        """Resolve a pull request's target branch, once, and store it.

        GitHub's run payload carries ``pull_requests`` only while that pull
        request is visible to the token. A run read after it was closed comes
        back with ``pr_number`` but no base entry, so the same object reports a
        PR number and ``base_ref: null`` -- two facts about one thing, one of
        them missing, which reads as a bug in whoever is looking at it.

        The base branch lives on the pull request, one call away, and it does not
        change once the PR is merged. So: fetch it, write it back, and let every
        later read be free.

        Best effort. A failure here leaves ``base_ref`` unknown and changes
        nothing else -- it must never fail a read that already succeeded.
        """
        if record.base_ref or record.pr_number is None:
            return
        try:
            pull = await self._repositories.get_pull_request(repository, record.pr_number)
        except Exception:  # noqa: BLE001 - an unknown base branch must not fail the read
            logger.debug(
                "could not resolve base_ref for run %s (PR #%s)",
                run.github_run_id,
                record.pr_number,
            )
            return
        base = pull.get("base") if isinstance(pull, Mapping) else None
        ref = str(base.get("ref") or "").strip() if isinstance(base, Mapping) else ""
        if not ref:
            return
        record.base_ref = ref
        run.base_ref = ref
        try:
            await run.save()
        except Exception:  # noqa: BLE001 - the answer is already correct in memory
            logger.warning("could not store base_ref for run %s", run.github_run_id)

    async def _attempt_record(
        self, run: AsyncWorkflowRun, repo_full_name: str, attempt: AsyncRunAttempt
    ) -> RunRecord:
        """One run's view, carrying the state of the attempt that was asked for.

        The run-level fields stay the run's (branch, commit, PR); only the
        execution's own state -- status, conclusion, times -- comes from the
        attempt, which is also the only attempt whose jobs are attached.
        """
        record = _to_run_record(run, repo_full_name)
        # The workflow is a run-level fact living in its own table -- the run row
        # carries only ``workflow_id`` -- and :meth:`records` resolves it in one
        # batch. This path builds a single record and used to skip that lookup,
        # so the same run answered ``workflow: null`` for an old attempt and with
        # the name for the current one, purely because of which call was made.
        workflows = await self._store.workflows_by_id([run.workflow_id])
        record.workflow_name, record.workflow_path = workflows.get(run.workflow_id, ("", ""))
        record.attempt = attempt.attempt_number
        record.status = attempt.status or "queued"
        record.conclusion = attempt.conclusion
        record.started_at = as_utc(attempt.started_at)
        record.completed_at = as_utc(attempt.completed_at)
        record.is_latest = attempt.attempt_number == (run.latest_attempt or 0)

        cached, has_jobs = (await self._store.cache_facts([run.id or 0])).get(
            run.id or 0, (0, False)
        )
        record.attempts_cached = cached
        record.cache_state = _cache_state(
            run,
            attempts_total=record.attempts_total,
            attempts_cached=cached,
            newest_attempt_has_jobs=has_jobs,
        )

        jobs = await run.jobs_of_attempt(attempt.attempt_number)
        record.jobs = [_to_job_record(job, run, await self._store.steps(job)) for job in jobs]
        return record

    async def attach_failed_jobs(self, records: Sequence[RunRecord]) -> None:
        """Fill ``failed_jobs`` on these records, with one query for all of them.

        The batch exists to answer "which repository is red, and where". One
        query over the latest attempts of the whole batch is the difference
        between that and N calls to ``get_run``.
        """
        if not records:
            return
        run_ids = [record.run_id for record in records]
        # Records carry the GitHub run id; the row ids are what the query needs.
        rows = await self._store.runs_by_github_ids(run_ids)
        by_github_id = {run.github_run_id: run for run in rows if run.id is not None}
        failed = await self._store.failed_jobs_by_run(
            [run.id or 0 for run in by_github_id.values()]
        )
        for record in records:
            run = by_github_id.get(record.run_id)
            entries = failed.get(run.id or 0, []) if run is not None else []
            record.failed_jobs = [JobFailure(name=name, conclusion=c) for name, c in entries]

    async def records(
        self,
        runs: Sequence[AsyncWorkflowRun],
        repo_full_name: str,
        *,
        with_jobs: bool = True,
    ) -> list[RunRecord]:
        """Build the views of ``runs``, in the same order.

        The cache facts cost two grouped queries for the whole batch, never one
        per run: a summary that claims to be cheap cannot go and read fifteen
        repositories' attempt tables row by row.
        """
        if not runs:
            return []

        records = [_to_run_record(run, repo_full_name) for run in runs]
        workflows = await self._store.workflows_by_id([run.workflow_id for run in runs])
        for record, run in zip(records, runs, strict=True):
            name, path = workflows.get(run.workflow_id, ("", ""))
            record.workflow_name = name
            record.workflow_path = path
        run_ids = [run.id or 0 for run in runs]
        facts = await self._store.cache_facts(run_ids)
        jobs = await self._store.jobs_by_run(run_ids) if with_jobs else {}

        for record, run in zip(records, runs, strict=True):
            cached, has_jobs = facts.get(run.id or 0, (0, False))
            record.attempts_cached = cached
            record.cache_state = _cache_state(
                run,
                attempts_total=record.attempts_total,
                attempts_cached=cached,
                newest_attempt_has_jobs=has_jobs,
            )
        if with_jobs:
            # One query for every step of every job on the page: the summary has
            # to say "this step's log is here", and the answer is one column.
            sizes = await self._store.step_log_sizes(
                [step.id or 0 for pairs in jobs.values() for _job, steps in pairs for step in steps]
            )
            for record, run in zip(records, runs, strict=True):
                record.jobs = [
                    _to_job_record(job, run, steps, log_sizes=sizes)
                    for job, steps in jobs.get(run.id or 0, [])
                ]
        return records


def _clamp(per_page: int) -> int:
    """Clamp a page size into the ``[1, MAX_PER_PAGE]`` range."""
    if per_page < 1:
        return DEFAULT_PER_PAGE
    return min(per_page, MAX_PER_PAGE)


def _tail(content: str, tail: int | None) -> str:
    """Return the last ``tail`` lines of ``content`` (``None`` keeps it all)."""
    if tail is None or tail <= 0:
        return content
    lines = content.splitlines()
    return "\n".join(lines[-tail:])


__all__ = ["MAX_PER_PAGE", "QueryController"]
