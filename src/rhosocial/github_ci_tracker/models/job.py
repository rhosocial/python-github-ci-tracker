# src/rhosocial/github_ci_tracker/models/job.py
"""Workflow run job model, sync and async twins.

A job is one unit of work of a workflow run, mirrored locally from the
GitHub jobs payload. The single behavior here is the write path:
:meth:`Job.upsert_from_remote` is keyed by ``(attempt_id, github_job_id)``,
applies the forward-only state machine and cascades into the payload's
``steps`` array, storing every step through
:meth:`Step.upsert_from_remote
<rhosocial.github_ci_tracker.models.step.Step.upsert_from_remote>`.

Nothing here talks to GitHub: reading a log is orchestration (decide whether
the stored copy is enough, fetch it otherwise, store it), so it lives in
:meth:`QueryController.get_job_logs
<rhosocial.github_ci_tracker.controllers.query.QueryController.get_job_logs>`.
``ct_job_logs.job_id`` is UNIQUE, so a job has at most one log and a purged log
leaves the job untouched -- see :mod:`.log`.

The twins are behavior-identical; the sync one exists for the project's
sync/async parity rule. References that would make the import graph cyclic
-- the run, step, log and repository models plus the ``core`` package
(whose ``__init__`` imports the event handlers, which import the models) --
are imported lazily inside method bodies; the type annotations use
``TYPE_CHECKING`` imports instead.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from rhosocial.activerecord.backend.errors import IntegrityError
from rhosocial.activerecord.base.field_proxy import FieldProxy
from rhosocial.activerecord.field.timestamp import DefaultTimestampMixin
from rhosocial.activerecord.model import ActiveRecord, AsyncActiveRecord
from rhosocial.activerecord.query import ActiveQuery, AsyncActiveQuery
from rhosocial.activerecord.relation.async_descriptors import AsyncBelongsTo, AsyncHasOne
from rhosocial.activerecord.relation.descriptors import BelongsTo, HasOne

from .relations import AsyncOrderedHasMany, OrderedHasMany

if TYPE_CHECKING:
    from .attempt import AsyncRunAttempt, RunAttempt
    from .log import AsyncJobLog, JobLog
    from .run import AsyncWorkflowRun, WorkflowRun
    from .step import AsyncStep, Step

logger = logging.getLogger(__name__)


def _as_int(value: Any) -> int | None:
    """Coerce a GitHub scalar to ``int``, returning ``None`` when impossible."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_str(value: Any, default: str = "") -> str:
    """Return a trimmed string, falling back to ``default`` for blank values."""
    if value is None:
        return default
    text = str(value).strip()
    return text or default


class Job(DefaultTimestampMixin, ActiveRecord):
    """A job within a workflow run, synchronous twin."""

    __table_name__ = "ct_jobs"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    #: GitHub job id. Globally unique -- a re-run issues *new* job ids rather
    #: than reusing the old ones -- so this alone determines the row.
    github_job_id: int = 0
    #: Which attempt of the run this job belongs to. Not a ``run_id``: an
    #: attempt determines exactly one run, so carrying it here would make
    #: ``attempt_id -> run_id`` a functional dependency whose determinant is not
    #: a superkey -- a BCNF violation, and one that leaves the two foreign keys
    #: free to disagree. See :mod:`rhosocial.github_ci_tracker.schema.model`.
    attempt_id: int = 0
    name: str = ""
    status: str = "queued"
    conclusion: str | None = None
    runner_name: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    #: Owning attempt (pairs with :attr:`RunAttempt.jobs`).
    attempt: ClassVar[BelongsTo[RunAttempt]] = BelongsTo(
        foreign_key="attempt_id", inverse_of="jobs"
    )
    #: This job's log, when one is stored. The optional side of the 1:1 is the
    #: absent row, so a purged log costs no UPDATE here (see :mod:`.log`).
    job_log: ClassVar[HasOne[JobLog]] = HasOne(foreign_key="job_id", inverse_of="job")

    @property
    def is_finished(self) -> bool:
        """Whether this job has reached a terminal state."""
        return self.status == "completed"

    @property
    def is_failed(self) -> bool:
        """Whether this job finished with a failure.

        A failed job is the one whose log is worth fetching eagerly, so the
        predicate belongs here rather than as a string comparison at each call
        site.
        """
        return self.status == "completed" and self.conclusion == "failure"

    #: Steps of this job, in step number order.
    steps: ClassVar[OrderedHasMany[Step]] = OrderedHasMany(
        foreign_key="job_id",
        inverse_of="job",
        order_key=lambda step: step.number,
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    def find_by_github_id(cls, github_job_id: int) -> Job | None:
        """Return the stored record of a GitHub job id, or ``None``."""
        return cls.find_one(cls.c.github_job_id == github_job_id)

    @classmethod
    def for_attempt(cls, attempt_id: int) -> ActiveQuery:
        """Query builder over the jobs of one attempt, id ascending.

        The builder is not terminated; callers may add further clauses
        before calling ``all()`` / ``one()`` / ``count()``. To read the jobs of
        a whole run, go through the run's attempts --
        ``WorkflowRun.jobs_of_attempt`` does that for its latest attempt.
        """
        return cls.query().where(cls.c.attempt_id == attempt_id).order_by((cls.c.id, "ASC"))

    # ---- behavior -------------------------------------------------------------

    # ``steps`` is the relation descriptor declared above; call it as
    # ``job.steps()`` to get the stored steps in number order.

    @classmethod
    def upsert_from_remote(cls, run: WorkflowRun, job_data: Mapping[str, Any]) -> Job:
        """Create or update a job record from a remote job payload.

        The lookup is keyed by ``(attempt_id, github_job_id)``:

        1. a stored job moves through the forward-only state machine (a
           stale or out-of-order payload is logged and skipped, never
           fatal) and is refreshed in place (name, runner, timestamps);
        2. otherwise the payload is stored as a new row.

        A ``steps`` array in the payload is cascaded: every entry is
        upserted for the stored job through :meth:`Step.upsert_from_remote
        <rhosocial.github_ci_tracker.models.step.Step.upsert_from_remote>`.

        Args:
            run: The persisted run record the job belongs to. Only its id and
                its latest ``run_attempt`` are read; the job hangs off the
                attempt, not off the run.
            job_data: A GitHub job payload (REST object or webhook
                fragment), optionally carrying a ``steps`` array.

        Returns:
            The persisted :class:`Job` record.
        """
        from .attempt import RunAttempt
        from .errors import InvalidStateTransitionError
        from .states import (
            KIND_JOB,
            StateMachine,
            normalize_conclusion,
            normalize_status,
            parse_timestamp,
        )
        from .step import Step

        github_job_id = _as_int(job_data.get("id")) or 0
        # The payload's own attempt is authoritative; a webhook fragment that
        # carries no attempt belongs to the newest one this run has stored.
        latest = RunAttempt.latest_for_run(run.id or 0)
        job_attempt = _as_int(job_data.get("run_attempt")) or (
            latest.attempt_number if latest else 1
        )
        # The foreign key is NOT NULL, so the attempt row has to exist before the
        # job can. A webhook fragment names the attempt without describing it,
        # hence ``ensure`` rather than "find".
        attempt = RunAttempt.ensure(run, job_attempt)

        job = cls.find_one(
            (cls.c.attempt_id == attempt.id) & (cls.c.github_job_id == github_job_id)
        )

        status = normalize_status(job_data.get("status"))
        conclusion = normalize_conclusion(job_data.get("conclusion"))
        started_at = parse_timestamp(job_data.get("started_at"))
        completed_at = parse_timestamp(job_data.get("completed_at"))
        name = _clean_str(job_data.get("name"))
        runner_name = _clean_str(job_data.get("runner_name")) or None

        inserted = False
        if job is None:
            job = cls(
                attempt_id=attempt.id or 0,
                github_job_id=github_job_id,
                name=name,
                status=status,
                conclusion=conclusion,
                runner_name=runner_name,
                started_at=started_at,
                completed_at=completed_at,
            )
            try:
                job.save()
                inserted = True
            except IntegrityError:
                winner = cls.find_one(
                    (cls.c.attempt_id == (attempt.id or 0)) & (cls.c.github_job_id == github_job_id)
                )
                if winner is None:
                    raise
                job = winner

        if not inserted:
            try:
                StateMachine.apply_transition(job, status, conclusion, KIND_JOB)
            except InvalidStateTransitionError as exc:
                logger.warning("Ignoring out-of-order status for job %s: %s", github_job_id, exc)
            if name:
                job.name = name
            job.runner_name = runner_name or job.runner_name
            if started_at is not None:
                job.started_at = started_at
            if completed_at is not None:
                job.completed_at = completed_at
            if job.is_dirty:
                job.save()

        for step_data in job_data.get("steps") or []:
            Step(job_id=job.id or 0).upsert_from_remote(step_data)
        return job


class AsyncJob(DefaultTimestampMixin, AsyncActiveRecord):
    """A job within a workflow run, asynchronous twin.

    Behavior-identical to :class:`Job`; every method performs the same
    requests and raises the same exceptions.
    """

    __table_name__ = "ct_jobs"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    #: GitHub job id. Globally unique -- a re-run issues *new* job ids rather
    #: than reusing the old ones -- so this alone determines the row.
    github_job_id: int = 0
    #: Which attempt of the run this job belongs to. Not a ``run_id``: see
    #: :attr:`Job.attempt_id` for why carrying the run here would be a BCNF
    #: violation.
    attempt_id: int = 0
    name: str = ""
    status: str = "queued"
    conclusion: str | None = None
    runner_name: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    #: Owning attempt (pairs with :attr:`AsyncRunAttempt.jobs`).
    attempt: ClassVar[AsyncBelongsTo[AsyncRunAttempt]] = AsyncBelongsTo(
        foreign_key="attempt_id", inverse_of="jobs"
    )

    @property
    def is_finished(self) -> bool:
        """Whether this job has reached a terminal state.

        See :attr:`Job.is_finished`; the same predicate.
        """
        return self.status == "completed"

    @property
    def is_failed(self) -> bool:
        """Whether this job finished with a failure.

        See :attr:`Job.is_failed`; the same predicate, so the two paths cannot
        disagree about which jobs are worth a log fetch.
        """
        return self.status == "completed" and self.conclusion == "failure"

    #: This job's log, when one is stored. The optional side of the 1:1 is the
    #: absent row, so a purged log costs no UPDATE here (see :mod:`.log`).
    job_log: ClassVar[AsyncHasOne[AsyncJobLog]] = AsyncHasOne(
        foreign_key="job_id", inverse_of="job"
    )
    #: Steps of this job, in step number order.
    steps: ClassVar[AsyncOrderedHasMany[AsyncStep]] = AsyncOrderedHasMany(
        foreign_key="job_id",
        inverse_of="job",
        order_key=lambda step: step.number,
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    async def find_by_github_id(cls, github_job_id: int) -> AsyncJob | None:
        """Return the stored record of a GitHub job id, or ``None``."""
        return await cls.find_one(cls.c.github_job_id == github_job_id)

    @classmethod
    def for_attempt(cls, attempt_id: int) -> AsyncActiveQuery:
        """Query builder over the jobs of one attempt, id ascending.

        See :meth:`Job.for_attempt`; the builder is not terminated.
        """
        return cls.query().where(cls.c.attempt_id == attempt_id).order_by((cls.c.id, "ASC"))

    # ---- behavior -------------------------------------------------------------

    # ``steps`` is the relation descriptor declared above; call it as
    # ``job.steps()`` to get the stored steps in number order.

    @classmethod
    async def upsert_from_remote(
        cls, run: AsyncWorkflowRun, job_data: Mapping[str, Any]
    ) -> AsyncJob:
        """Create or update a job record from a remote job payload.

        See :meth:`Job.upsert_from_remote` for the
        ``(attempt_id, github_job_id)`` key resolution, the state machine
        semantics and the ``steps`` cascade; the stored field semantics are
        identical.
        """
        from .attempt import AsyncRunAttempt
        from .errors import InvalidStateTransitionError
        from .states import (
            KIND_JOB,
            StateMachine,
            normalize_conclusion,
            normalize_status,
            parse_timestamp,
        )
        from .step import AsyncStep

        github_job_id = _as_int(job_data.get("id")) or 0
        # See :meth:`Job.upsert_from_remote`: the payload's attempt wins, and a
        # fragment without one belongs to the newest stored attempt.
        latest = await AsyncRunAttempt.latest_for_run(run.id or 0)
        job_attempt = _as_int(job_data.get("run_attempt")) or (
            latest.attempt_number if latest else 1
        )
        # The foreign key is NOT NULL, so the attempt row must exist first.
        attempt = await AsyncRunAttempt.ensure(run, job_attempt)

        job = await cls.find_one(
            (cls.c.attempt_id == attempt.id) & (cls.c.github_job_id == github_job_id)
        )

        status = normalize_status(job_data.get("status"))
        conclusion = normalize_conclusion(job_data.get("conclusion"))
        started_at = parse_timestamp(job_data.get("started_at"))
        completed_at = parse_timestamp(job_data.get("completed_at"))
        name = _clean_str(job_data.get("name"))
        runner_name = _clean_str(job_data.get("runner_name")) or None

        inserted = False
        if job is None:
            job = cls(
                attempt_id=attempt.id or 0,
                github_job_id=github_job_id,
                name=name,
                status=status,
                conclusion=conclusion,
                runner_name=runner_name,
                started_at=started_at,
                completed_at=completed_at,
            )
            try:
                await job.save()
                inserted = True
            except IntegrityError:
                # ``queued`` and ``in_progress`` for the same job can be
                # delivered at the same moment: both read "no row", both insert,
                # and the unique constraint rejects the loser. The row that won
                # is the one to update, not an error to report.
                winner = await cls.find_one(
                    (cls.c.attempt_id == (attempt.id or 0)) & (cls.c.github_job_id == github_job_id)
                )
                if winner is None:
                    raise
                job = winner

        if not inserted:
            # A stored job (or the winner of the insert race) moves through the
            # forward-only state machine: a stale or out-of-order payload is
            # logged and skipped, never fatal.
            try:
                StateMachine.apply_transition(job, status, conclusion, KIND_JOB)
            except InvalidStateTransitionError as exc:
                logger.warning("Ignoring out-of-order status for job %s: %s", github_job_id, exc)
            if name:
                job.name = name
            job.runner_name = runner_name or job.runner_name
            if started_at is not None:
                job.started_at = started_at
            if completed_at is not None:
                job.completed_at = completed_at
            if job.is_dirty:
                await job.save()

        for step_data in job_data.get("steps") or []:
            await AsyncStep(job_id=job.id or 0).upsert_from_remote(step_data)
        return job
