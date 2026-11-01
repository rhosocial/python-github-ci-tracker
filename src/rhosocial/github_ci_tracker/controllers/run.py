# src/rhosocial/github_ci_tracker/controllers/run.py
"""The run controller: every flow about a run, and the rows they are built from.

A controller is a group of flows. :class:`RunController` answers "adopt or
update this run from GitHub" and "answer with the run a caller named";
:class:`RunData` below is the row layer those flows are assembled from -- the
run, its attempts and their jobs and steps, kept together because the run's
upsert is what resolves the attempt a job hangs off. A class per table would
make every caller re-derive that order.

Nothing here writes a query by hand: ``find_one``, ``query()`` and the model's
own ``adopt_from_remote`` / ``update_from_remote`` / ``sync_jobs_from_remote``
are the whole surface."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime

from ..core.exceptions import RepositoryNotFoundError, RunNotFoundError
from ..models import (
    AsyncJob,
    AsyncRepository,
    AsyncStep,
    AsyncWorkflow,
    AsyncWorkflowRun,
)
from ..models.attempt import AsyncRunAttempt
from ..models.errors import CitrackerError  # noqa: F401 - re-exported for callers
from ..models.log import AsyncJobLog, AsyncStepLog
from ..models.states import FAILED_CONCLUSIONS
from ..services.github.protocols import RunReader
from ..views import DurationSample

__all__ = [
    "RunController",
    "RunData",
]


#: Conclusions that are not a failure to report: a green job, and the ones
#: GitHub reports for work that was deliberately not run.
_PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})

#: How many failing jobs one run reports in a list. More than a handful means
#: the answer is "almost everything failed", and the names stop adding signal.
FAILED_JOBS_PER_RUN = 10


class RunController:
    """Track runs: fetch, store, read back.

    Typed against :class:`~.github.protocols.RunReader`, so it cannot dispatch or
    cancel -- those spend CI minutes and belong to :class:`DispatchController`.
    """

    def __init__(self, *, runs: RunReader, runs_data: RunData) -> None:
        """Build the controller.

        Args:
            runs: Anything satisfying ``RunReader``: the HTTP group in production,
                a recording fake in a test.
            runs_data: The controller that owns run storage.
        """
        self._runs = runs
        self._store = runs_data

    async def repository(self, repo_full_name: str) -> AsyncRepository:
        """The tracked repository called ``repo_full_name``.

        Raises:
            RepositoryNotFoundError: Nothing is subscribed under that name.
        """
        repo = await self._store.repository(repo_full_name)
        if repo is None:
            raise RepositoryNotFoundError(f"Repository '{repo_full_name}' is not tracked")
        return repo

    async def fetch(
        self,
        repo: AsyncRepository,
        run_id: int,
        *,
        with_jobs: bool = True,
    ) -> AsyncWorkflowRun:
        """Read a run from GitHub and store it, jobs included.

        Args:
            repo: The repository the run must belong to.
            run_id: GitHub run id.
            with_jobs: Read and store the run's jobs (and their steps) too. A
                caller that only needs the run's state saves one API call.

        Returns:
            The stored run.

        Raises:
            RunNotFoundError: The stored run belongs to another repository.
            GithubAPIError: GitHub refused the read.
        """
        payload = await self._runs.get_run(repo.full_name, run_id)

        run = await self._store.run(run_id)
        if run is None:
            run = await self._store.adopt(repo, payload)
        else:
            await self._require_scope(run, repo)
            run = await self._store.update(run, payload)

        if with_jobs:
            jobs = await self._runs.get_run_jobs(repo.full_name, run_id)
            await self._store.sync_jobs(run, jobs)
        return run

    async def fetch_by_name(
        self,
        repo_full_name: str,
        run_id: int,
        *,
        with_jobs: bool = True,
    ) -> AsyncWorkflowRun:
        """Same as :meth:`fetch`, resolving the repository by name first."""
        return await self.fetch(await self.repository(repo_full_name), run_id, with_jobs=with_jobs)

    async def get(
        self,
        repo_full_name: str,
        run_id: int,
        *,
        refresh: bool = False,
    ) -> AsyncWorkflowRun:
        """Answer with the stored run, fetching it first when needed.

        A run that is unknown locally is adopted: the cache is sparse, and
        reporting "not found" for something GitHub has is the behaviour that made
        ``rerun`` fail on runs it could have refreshed.

        Args:
            repo_full_name: ``owner/repo``.
            run_id: GitHub run id.
            refresh: Fetch from GitHub even when a local row exists. Without it a
                known run is answered from the cache, which the push channel
                exists to keep current.

        Raises:
            RepositoryNotFoundError: The repository is not tracked.
            RunNotFoundError: The run belongs to another repository.
            GithubAPIError: A required fetch failed.
        """
        repo = await self.repository(repo_full_name)

        run = await self._store.run(run_id)
        if run is not None:
            await self._require_scope(run, repo)
            if not refresh:
                return run

        return await self.fetch(repo, run_id, with_jobs=refresh)

    async def duration_sample(
        self,
        workflow_id: int,
        *,
        samples: int = 10,
    ) -> DurationSample | None:
        """How long this workflow's recent finished runs took.

        The caller's question is "how long should I wait", and a run completes
        when it completes -- a failure that dies in twenty seconds belongs in the
        sample as much as a pass that takes four minutes. Sampling only the
        successes would under-budget exactly the workflows that fail often.

        Terminal rows only: a run still in flight has an ``end`` of "now", which
        would make the sample drift with whenever it happens to be read.

        Args:
            workflow_id: The stored workflow whose runs to sample.
            samples: How many of the newest finished runs to use.

        Returns:
            The percentiles, or ``None`` when no finished run has both
            timestamps (a fresh subscription, or rows from before the columns
            were filled).
        """
        ids = await self._store.recent_run_ids([workflow_id], limit=max(samples * 4, 20))
        if not ids:
            return None
        rows = {row.id: row for row in await self._store.runs_by_ids(ids)}

        durations: list[float] = []
        for run_id in ids:
            row = rows.get(run_id)
            if row is None or row.latest_status != "completed":
                continue
            if row.latest_started_at is None or row.latest_completed_at is None:
                continue
            durations.append((row.latest_completed_at - row.latest_started_at).total_seconds())
            if len(durations) >= samples:
                break

        if not durations:
            return None
        durations.sort()
        return DurationSample(
            samples=len(durations),
            p50_seconds=self._percentile(durations, 0.50),
            p90_seconds=self._percentile(durations, 0.90),
        )

    @staticmethod
    def _percentile(ordered: Sequence[float], fraction: float) -> float:
        """Nearest-rank percentile of an ascending sample.

        With a handful of runs the interpolated variants differ by seconds; the
        point of the figure is to size a timeout, not to describe a distribution
        precisely.
        """
        index = max(0, math.ceil(fraction * len(ordered)) - 1)
        return ordered[min(index, len(ordered) - 1)]

    async def _require_scope(self, run: AsyncWorkflowRun, repo: AsyncRepository) -> None:
        """Refuse a run whose workflow belongs to another repository.

        Raises:
            RunNotFoundError: The run is not this repository's.
        """
        workflow = await self._store.workflow_of(run)
        if workflow is None or workflow.repository_id != repo.id:
            raise RunNotFoundError(
                f"Run {run.github_run_id} does not belong to repository '{repo.full_name}'"
            )


# -- the rows the flows above are built from ------------------------------


class RunData:
    """The repository / workflow / run aggregate, one method per question."""

    async def repository(self, full_name: str) -> AsyncRepository | None:
        """The tracked repository called ``full_name``, or ``None``."""
        return await AsyncRepository.find_one(AsyncRepository.c.full_name == full_name)

    async def repository_by_id(self, repository_id: int) -> AsyncRepository | None:
        """The tracked repository with this local id, or ``None``."""
        return await AsyncRepository.find_one(AsyncRepository.c.id == repository_id)

    async def repositories(self, names: Sequence[str]) -> dict[str, AsyncRepository]:
        """The tracked repositories among ``names``, keyed by full name.

        One query for the whole batch: a caller answering about fifteen
        repositories must not read the table fifteen times.
        """
        if not names:
            return {}
        rows = await (
            AsyncRepository.query().where(AsyncRepository.c.full_name.in_(list(names))).all()
        )
        return {repo.full_name: repo for repo in rows}

    async def run(self, github_run_id: int, *, with_state: bool = True) -> AsyncWorkflowRun | None:
        """The stored run with this GitHub id, or ``None``.

        ``with_state`` loads the derived ``latest_*`` attributes (the state of
        the newest attempt) in the same query. It is the default because every
        caller that asks about a run by id wants to know how it went; a caller
        that only needs identity can turn it off.
        """
        return await AsyncWorkflowRun.find_by_github_id(github_run_id, derived=with_state)

    async def repository_of(self, run: AsyncWorkflowRun) -> AsyncRepository | None:
        """The repository a run belongs to, or ``None`` when the row is gone."""
        workflow = await self.workflow_of(run)
        if workflow is None:
            return None
        return await AsyncRepository.find_one(AsyncRepository.c.id == workflow.repository_id)

    async def note_webhook_received(self, run: AsyncWorkflowRun) -> AsyncWorkflowRun:
        """Stamp a run with the arrival of a ``workflow_run`` delivery.

        Only the run stream stamps it: the job stream has its own ledger rows,
        and writing the run's signal from a job event made the two
        indistinguishable.
        """
        from datetime import datetime, timezone

        run.webhook_received_at = datetime.now(timezone.utc)
        await run.save()
        return run

    async def workflow_of(self, run: AsyncWorkflowRun) -> AsyncWorkflow | None:
        """The workflow a run belongs to, or ``None`` when the row is gone."""
        return await AsyncWorkflow.find_one(AsyncWorkflow.c.id == run.workflow_id)

    async def workflow(self, repo: AsyncRepository, reference: str) -> AsyncWorkflow:
        """The repository's workflow matching ``reference``.

        The matching itself is the model's: a path, a file name, a display name
        or a numeric id, in that order.

        Raises:
            WorkflowNotFoundError: Nothing matches; the message lists what the
                repository does have.
        """
        return await AsyncWorkflow.resolve(repo.id or 0, reference)

    def belongs_to_pull_request(
        self, payload: Mapping[str, object], number: int, head_sha: str
    ) -> bool:
        """Whether a run payload belongs to pull request ``number``.

        The attribution rules are the model's (see
        :meth:`AsyncWorkflowRun.belongs_to_pull_request`); the controller only
        makes them reachable without this module importing the model.
        """
        return AsyncWorkflowRun.belongs_to_pull_request(payload, number, head_sha)

    async def adopt(self, repo: AsyncRepository, payload: Mapping[str, object]) -> AsyncWorkflowRun:
        """Store a run GitHub knows and the database does not.

        The payload goes to the model unchanged: it creates the workflow if
        needed, normalizes the state and writes the attempt row the jobs hang off.
        """
        return await AsyncWorkflowRun.adopt_from_remote(repo, payload)

    async def update(
        self, run: AsyncWorkflowRun, payload: Mapping[str, object]
    ) -> AsyncWorkflowRun:
        """Apply a payload to a stored run, through the model's state machine."""
        return await run.update_from_remote(payload)

    async def store_attempt(
        self, run: AsyncWorkflowRun, payload: Mapping[str, object]
    ) -> AsyncRunAttempt:
        """Upsert the attempt a payload describes; the run's state is untouched.

        Used when a caller asks for a *past* attempt. The run's derived state
        must keep pointing at the newest execution, so this writes the attempt
        row and nothing else.
        """
        return await AsyncRunAttempt.upsert_from_remote(run, payload)

    async def sync_jobs(
        self, run: AsyncWorkflowRun, jobs: Sequence[Mapping[str, object]]
    ) -> list[int]:
        """Store a batch of job payloads under their own attempts."""
        return await run.sync_jobs_from_remote(jobs)

    # -- reading a repository's runs -------------------------------------------

    async def workflows(self, repo: AsyncRepository) -> list[AsyncWorkflow]:
        """Every cached workflow of ``repo``."""
        return await AsyncWorkflow.for_repository(repo.id or 0).all()

    async def runs(
        self,
        workflow_ids: Sequence[int],
        *,
        branch: str | None = None,
        commit_sha: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AsyncWorkflowRun]:
        """Stored runs of these workflows, newest first, identity only.

        No status or conclusion filter: those are facts about an attempt, not a
        column here. A caller that needs the state uses :meth:`runs_by_ids`
        (which loads it) and filters the returned rows.

        ``commit_sha`` shorter than 40 characters is a prefix search, which is
        what makes an abbreviated sha useful.
        """
        if not workflow_ids:
            return []

        query = AsyncWorkflowRun.query().where(
            AsyncWorkflowRun.c.workflow_id.in_(list(workflow_ids))
        )
        if branch:
            query = query.where(AsyncWorkflowRun.c.branch == branch)
        if commit_sha:
            sha = commit_sha.strip().lower()
            if len(sha) >= 40:
                query = query.where(AsyncWorkflowRun.c.commit_sha == sha)
            else:
                query = query.where(AsyncWorkflowRun.c.commit_sha.like(f"{sha}%"))

        query = query.order_by((AsyncWorkflowRun.c.run_number, "DESC"))
        if limit is not None:
            query = query.limit(limit)
        if offset:
            query = query.offset(offset)
        return await query.all()

    async def runs_by_ids(self, run_ids: Sequence[int]) -> list[AsyncWorkflowRun]:
        """The stored runs with these ids, each carrying its newest attempt's state.

        One query for the whole batch, with the derived columns loaded, which is
        what a read path needs after it has upserted a page of payloads: the page
        order comes from GitHub, the state comes from the row.
        """
        ids = [run_id for run_id in run_ids if run_id]
        if not ids:
            return []
        return await AsyncWorkflowRun.find_all(AsyncWorkflowRun.c.id.in_(ids), derived=True)

    async def recent_run_ids(self, workflow_ids: Sequence[int], limit: int = 10) -> list[int]:
        """The ids of these workflows' most recently stored runs.

        Ids only: a caller asking "how did this job go lately" needs to know
        *which* runs to look at, not their payloads. Ordered by local id, which
        follows the order runs were adopted -- the newest row is the one this
        service learned about last.
        """
        if not workflow_ids:
            return []
        rows = await (
            AsyncWorkflowRun.query()
            .select(AsyncWorkflowRun.c.id)
            .where(AsyncWorkflowRun.c.workflow_id.in_(list(workflow_ids)))
            .order_by((AsyncWorkflowRun.c.id, "DESC"))
            .limit(limit)
            .all()
        )
        return [row.id for row in rows if row.id is not None]

    async def workflows_by_id(self, workflow_ids: Sequence[int]) -> dict[int, tuple[str, str]]:
        """The ``(name, path)`` of each workflow id, one query for the batch.

        A run carries only ``workflow_id``; the name and path live on the
        workflow row. Resolving them per run would put a query behind every
        summary line, which is exactly the cost a list cannot afford.
        """
        ids = [workflow_id for workflow_id in workflow_ids if workflow_id]
        if not ids:
            return {}
        rows = await AsyncWorkflow.find_all(AsyncWorkflow.c.id.in_(ids))
        return {row.id or 0: (row.name, row.path) for row in rows if row.id is not None}

    async def remember_workflow_id(self, workflow: AsyncWorkflow, github_workflow_id: int) -> None:
        """Store the GitHub id a workflow row was missing.

        A row written from a payload that carried no ``workflow_id`` has ``0``
        there, and a ``0`` silently disables the workflow filter when a caller
        asks for one. Filling it in at the first successful resolution keeps
        that from happening twice.
        """
        if workflow.id is None or not github_workflow_id:
            return
        workflow.workflow_id = github_workflow_id
        await workflow.save()

    async def count(self) -> int:
        """How many runs are stored."""
        return await AsyncWorkflowRun.query().count()

    async def added_since(self, cutoff: datetime) -> int:
        """How many run rows this process stored since ``cutoff``.

        ``created_at`` on a run row is the **ingest** time, not the run's own
        age (that is the attempt's ``github_created_at``). That makes it the
        right column for "how fast is this database filling": a backfill and a
        normal webhook trickle look identical in a total count, and a caller
        reading only totals cannot tell "still loading" from "lost data".

        Args:
            cutoff: An aware datetime; rows inserted at or after it are counted.
        """
        return await AsyncWorkflowRun.query().where(AsyncWorkflowRun.c.created_at >= cutoff).count()

    async def latest(
        self, workflow_ids: Sequence[int], *, branch: str | None = None
    ) -> AsyncWorkflowRun | None:
        """The newest stored run of these workflows, or ``None``."""
        rows = await self.runs(workflow_ids, branch=branch, limit=1)
        return rows[0] if rows else None

    async def belongs_to(self, run: AsyncWorkflowRun, repo: AsyncRepository) -> bool:
        """Whether this run is one of ``repo``'s.

        A run id is globally unique, so a caller holding one should not have to
        remember which repository it belongs to -- but a caller *naming* a
        repository must not be answered with another repository's run.
        """
        workflow = await self.workflow_of(run)
        return workflow is not None and workflow.repository_id == repo.id

    async def set_default_branch(self, repo: AsyncRepository, branch: str) -> AsyncRepository:
        """Record the default branch GitHub reported, when it changed."""
        if branch and branch != repo.default_branch:
            repo.default_branch = branch
            await repo.save()
        return repo

    async def jobs_by_run(
        self, run_ids: Sequence[int]
    ) -> dict[int, list[tuple[AsyncJob, list[AsyncStep]]]]:
        """Each run's jobs and their steps, grouped by local run id.

        Only the latest attempt of each run: GitHub's own ``/jobs`` endpoint
        returns just that, and mixing attempts would report the same job twice.
        Three queries for the whole batch, whatever the number of runs.
        """
        ids = [run_id for run_id in run_ids if run_id]
        if not ids:
            return {}

        attempts = await (
            AsyncRunAttempt.query()
            .select(
                AsyncRunAttempt.c.id, AsyncRunAttempt.c.run_id, AsyncRunAttempt.c.attempt_number
            )
            .where(AsyncRunAttempt.c.run_id.in_(ids))
            .all()
        )
        latest: dict[int, tuple[int, int]] = {}
        for attempt in attempts:
            current = latest.get(attempt.run_id)
            if current is None or attempt.attempt_number > current[0]:
                latest[attempt.run_id] = (attempt.attempt_number, attempt.id)

        attempt_to_run = {attempt_id: run_id for run_id, (_, attempt_id) in latest.items()}
        grouped: dict[int, list[AsyncJob]] = {run_id: [] for run_id in ids}
        if attempt_to_run:
            jobs = await (
                AsyncJob.query()
                .where(AsyncJob.c.attempt_id.in_(list(attempt_to_run)))
                .with_("steps")
                .order_by((AsyncJob.c.id, "ASC"))
                .all()
            )
            for job in jobs:
                run_id = attempt_to_run.get(job.attempt_id)
                if run_id is not None:
                    grouped.setdefault(run_id, []).append(job)

        # Nested comprehension on purpose: ``await`` cannot appear inside a
        # comprehension nested in another one before Python 3.11, and this
        # project supports 3.10.
        by_run: dict[int, list[tuple[AsyncJob, list[AsyncStep]]]] = {}
        for run_id, jobs in grouped.items():
            by_run[run_id] = [(job, await job.steps()) for job in jobs]
        return by_run

    async def runs_by_github_ids(self, github_run_ids: Sequence[int]) -> list[AsyncWorkflowRun]:
        """The stored rows for these GitHub run ids, identity only.

        A caller holding a view has the GitHub id, not the row id; this is the
        bridge back to the table without loading state it will not use.
        """
        ids = [run_id for run_id in github_run_ids if run_id]
        if not ids:
            return []
        return await AsyncWorkflowRun.find_all(AsyncWorkflowRun.c.github_run_id.in_(ids))

    async def failed_jobs_by_run(self, run_ids: Sequence[int]) -> dict[int, list[tuple[str, str]]]:
        """Each run's *failing* jobs, as ``(name, conclusion)``, one query.

        The question a batch exists to answer is "which of these repositories is
        red, and where" -- a name and a conclusion for the jobs that did not
        pass is enough for that, and a caller that needs the steps or the log
        was always going to call ``get_run`` anyway.

        Only the latest attempt of each run, like :meth:`jobs_by_run`: mixing
        attempts would list a job a re-run has already replaced.
        """
        ids = [run_id for run_id in run_ids if run_id]
        if not ids:
            return {}

        attempts = await (
            AsyncRunAttempt.query()
            .select(
                AsyncRunAttempt.c.id,
                AsyncRunAttempt.c.run_id,
                AsyncRunAttempt.c.attempt_number,
            )
            .where(AsyncRunAttempt.c.run_id.in_(ids))
            .all()
        )
        latest: dict[int, tuple[int, int]] = {}
        for attempt in attempts:
            current = latest.get(attempt.run_id)
            if current is None or attempt.attempt_number > current[0]:
                latest[attempt.run_id] = (attempt.attempt_number, attempt.id)

        attempt_to_run = {attempt_id: run_id for run_id, (_, attempt_id) in latest.items()}
        failed: dict[int, list[tuple[str, str]]] = {run_id: [] for run_id in ids}
        if not attempt_to_run:
            return failed

        jobs = await (
            AsyncJob.query()
            .select(AsyncJob.c.id, AsyncJob.c.attempt_id, AsyncJob.c.name, AsyncJob.c.conclusion)
            .where(AsyncJob.c.attempt_id.in_(list(attempt_to_run)))
            .order_by((AsyncJob.c.id, "ASC"))
            .all()
        )
        for job in jobs:
            conclusion = (job.conclusion or "").lower()
            # Only a conclusion that says "failed" counts. GitHub leaves the
            # conclusion unset while a job or step is still being materialized,
            # and "not passing" would report that as a failure.
            if conclusion not in FAILED_CONCLUSIONS:
                continue
            run_id = attempt_to_run.get(job.attempt_id)
            if run_id is None:
                continue
            if len(failed[run_id]) >= FAILED_JOBS_PER_RUN:
                continue
            failed[run_id].append((job.name, conclusion))
        return failed

    async def cache_facts(self, run_ids: Sequence[int]) -> dict[int, tuple[int, bool]]:
        """What the local copy holds for each run: newest cached attempt, and whether it has jobs.

        One query per fact for the whole batch, never one per run: a summary of
        fifteen repositories must not read fifteen attempt tables row by row.

        Returns:
            ``run_id -> (newest cached attempt number, that attempt has jobs)``.
            A run with no attempt row at all maps to ``(0, False)``.
        """
        if not run_ids:
            return {}

        attempts = await (
            AsyncRunAttempt.query()
            .select(
                AsyncRunAttempt.c.run_id, AsyncRunAttempt.c.attempt_number, AsyncRunAttempt.c.id
            )
            .where(AsyncRunAttempt.c.run_id.in_(list(run_ids)))
            .all()
        )
        newest: dict[int, tuple[int, int]] = {}
        for row in attempts:
            current = newest.get(row.run_id)
            if current is None or row.attempt_number > current[0]:
                newest[row.run_id] = (row.attempt_number, row.id)

        filled = {attempt_id for _, attempt_id in newest.values()}
        with_jobs: set[int] = set()
        if filled:
            jobs = await (
                AsyncJob.query()
                .select(AsyncJob.c.attempt_id)
                .where(AsyncJob.c.attempt_id.in_(list(filled)))
                .all()
            )
            with_jobs = {row.attempt_id for row in jobs}

        return {
            run_id: (
                newest[run_id][0] if run_id in newest else 0,
                run_id in newest and newest[run_id][1] in with_jobs,
            )
            for run_id in run_ids
        }

    # -- jobs, steps and logs --------------------------------------------------

    async def job(self, github_job_id: int) -> AsyncJob | None:
        """The stored job with this GitHub id, or ``None``."""
        return await AsyncJob.find_one(AsyncJob.c.github_job_id == github_job_id)

    async def run_of(self, job: AsyncJob) -> AsyncWorkflowRun | None:
        """The run a job belongs to, or ``None`` when the row is gone.

        Two hops, because a job hangs off an **attempt**, not off the run: the
        job row carries ``attempt_id``, and a re-run gives one run several
        attempts. Reading ``job.run_id`` would be reading a column that does not
        exist.
        """
        attempt = await AsyncRunAttempt.find_one(AsyncRunAttempt.c.id == job.attempt_id)
        if attempt is None:
            return None
        return await AsyncWorkflowRun.find_one(AsyncWorkflowRun.c.id == attempt.run_id)

    async def latest_attempt(self, run: AsyncWorkflowRun) -> AsyncRunAttempt | None:
        """The newest attempt row of a run, or ``None``.

        The run row carries no status or timestamps of its own -- those live on
        the attempt -- and its ``latest_*`` derived fields are only filled by a
        read that asked for them. A flow that needs "has this finished, and
        when" reads the row instead of trusting a possibly-unloaded attribute.
        """
        if run.id is None:
            return None
        attempts = await (
            AsyncRunAttempt.query()
            .where(AsyncRunAttempt.c.run_id == run.id)
            .order_by((AsyncRunAttempt.c.attempt_number, "DESC"))
            .limit(1)
            .all()
        )
        return attempts[0] if attempts else None

    async def attempt_ids(self, run_ids: Sequence[int]) -> list[int]:
        """Every stored attempt id of these runs.

        A job hangs off an attempt, so "the jobs of these runs" is two hops:
        attempts first, then their jobs. Both reads are indexed.
        """
        if not run_ids:
            return []
        rows = await (
            AsyncRunAttempt.query()
            .select(AsyncRunAttempt.c.id)
            .where(AsyncRunAttempt.c.run_id.in_(list(run_ids)))
            .all()
        )
        return [row.id for row in rows if row.id is not None]

    async def job_conclusions(self, attempt_ids: Sequence[int], job_name: str) -> list[str | None]:
        """The conclusion of every job called ``job_name`` under these attempts.

        One entry per job row, so a value of ``None`` (still running) counts as
        an execution that has not failed -- what the caller does with that is the
        service's call.
        """
        if not attempt_ids:
            return []
        rows = await (
            AsyncJob.query()
            .select(AsyncJob.c.conclusion)
            .where((AsyncJob.c.attempt_id.in_(list(attempt_ids))) & (AsyncJob.c.name == job_name))
            .all()
        )
        return [row.conclusion for row in rows]

    async def jobs(self, run: AsyncWorkflowRun) -> list[AsyncJob]:
        """The jobs of a run's current attempt, steps already loaded.

        Only the latest attempt: GitHub's own ``/jobs`` endpoint returns just
        that, and mixing attempts would report the same job twice.
        """
        return await run.jobs_of_attempt()

    async def steps(self, job: AsyncJob) -> list[AsyncStep]:
        """A job's steps, in step order.

        A direct query rather than the relation: the relation can answer from a
        load it already made on this instance, and a caller that has just
        re-synced the job (the healing paths do) would then be handed the rows
        from before the write.
        """
        if job.id is None:
            return []
        return await (
            AsyncStep.query()
            .where(AsyncStep.c.job_id == job.id)
            .order_by((AsyncStep.c.number, "ASC"))
            .all()
        )

    async def step_count(self, job_ids: Sequence[int]) -> int:
        """How many steps are stored under these jobs."""
        if not job_ids:
            return 0
        return await AsyncStep.query().where(AsyncStep.c.job_id.in_(list(job_ids))).count()

    async def log(self, job: AsyncJob) -> str | None:
        """A job's stored log, or ``None`` when it was never fetched or was purged."""
        if job.id is None:
            return None
        stored = await AsyncJobLog.find_for_job(job.id)
        return None if stored is None else stored.log_content

    async def store_log(self, job: AsyncJob, content: str) -> None:
        """Store a job's whole log as one row, replacing any log already there."""
        if job.id is None:
            return
        await AsyncJobLog(job_id=job.id, log_content=content).store()

    # ---- step logs -----------------------------------------------------------

    async def step_log_texts(self, steps: Sequence[AsyncStep]) -> dict[int, str]:
        """The captured text of these steps, keyed by step number.

        One query for the batch: a job has ten-ish steps and a read that asked
        for them should not pay per row. Steps without a row are absent from the
        mapping, which is the honest shape -- see :class:`RunLogCapture`.
        """
        ids = [step.id for step in steps if step.id is not None]
        if not ids:
            return {}
        rows = await AsyncStepLog.find_all(AsyncStepLog.c.step_id.in_(ids))
        by_step_id = {row.step_id: row.log_content for row in rows}
        return {
            step.number: by_step_id[step.id]
            for step in steps
            if step.id is not None and step.id in by_step_id
        }

    async def step_log_sizes(self, step_ids: Sequence[int]) -> dict[int, int]:
        """Line count per step id, for the steps that have a captured log.

        One query for a whole job or run: a summary that says "this step has a
        log" must not ask the database once per step.
        """
        ids = [step_id for step_id in step_ids if step_id]
        if not ids:
            return {}
        rows = await AsyncStepLog.query().where(AsyncStepLog.c.step_id.in_(ids)).all()
        return {row.step_id: row.line_count for row in rows}

    async def step_log_count(self, steps: Sequence[AsyncStep]) -> int:
        """How many of these steps already have a captured log."""
        ids = [step.id for step in steps if step.id is not None]
        if not ids:
            return 0
        return await AsyncStepLog.query().where(AsyncStepLog.c.step_id.in_(ids)).count()

    async def store_step_log(self, step: AsyncStep, content: str) -> None:
        """Store one step's text, replacing any capture already there."""
        if step.id is None:
            return
        await AsyncStepLog(step_id=step.id, log_content=content).store()
