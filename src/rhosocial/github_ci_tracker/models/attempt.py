# src/rhosocial/github_ci_tracker/models/attempt.py
"""Workflow run attempt model, sync and async twins.

A *run* is one GitHub Actions workflow run id; an *attempt* is one execution of
it. GitHub reuses the run id when a run is re-run and increments
``run_attempt``, so the run id alone never identifies one try -- a fact this
project once got the wrong way round. ``TriggerController.rerun`` inferred that a
re-run created a *new* run and went looking for an unfamiliar one, which
returned an unrelated run whenever anything else was pushed to the same branch.
This table exists so that question has a stored answer instead of a guess.

What an attempt carries that the run row does not: ``created_at``,
``run_started_at`` and ``updated_at`` are per attempt and they differ. Measured
on a public run with two attempts, where attempt 1 started at ``07:32:41Z`` and
the run row had already been overwritten with attempt 2's ``08:55:41Z``. Before
this table existed, storing the run overwrote the earlier execution's state, and
the only thing distinguishing the two was the ``ct_jobs.run_attempt`` integer --
now a foreign key, ``ct_jobs.attempt_id``.

The forward-only state machine is deliberately **not** applied here. An attempt
is history: a late ``in_progress`` payload for attempt 1 arriving after attempt
2 has started is the past arriving out of order, not a contradiction, and
refusing it would lose the only record of that execution.

The twins are behavior-identical; the sync one exists for the project's
sync/async parity rule. References that would make the import graph cyclic --
the run and job models plus the ``core`` package -- are imported lazily inside
method bodies; the type annotations use ``TYPE_CHECKING`` imports instead.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from rhosocial.activerecord.base.field_proxy import FieldProxy
from rhosocial.activerecord.field.timestamp import DefaultTimestampMixin
from rhosocial.activerecord.model import ActiveRecord, AsyncActiveRecord
from rhosocial.activerecord.query import ActiveQuery, AsyncActiveQuery
from rhosocial.activerecord.relation.async_descriptors import AsyncBelongsTo
from rhosocial.activerecord.relation.descriptors import BelongsTo

from .relations import AsyncOrderedHasMany, OrderedHasMany

if TYPE_CHECKING:
    from .job import AsyncJob, Job
    from .run import AsyncWorkflowRun, WorkflowRun

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


def _actor_login(data: Mapping[str, Any]) -> str | None:
    """The actor login out of a payload, tolerant of the field being absent."""
    actor = data.get("actor")
    if isinstance(actor, Mapping):
        return _clean_str(actor.get("login")) or None
    return _clean_str(actor) or None


def _completed_at(data: Mapping[str, Any], status: str) -> Any:
    """The attempt's completion timestamp, tolerating a missing one.

    A completed execution does not always carry ``completed_at``: GitHub omits
    it on some finished runs (observed with cancelled or superseded attempts),
    and a null there would make a duration computed from "now" grow without
    bound. ``updated_at`` is the last state change, which for a finished
    execution is its completion, so it is the fallback.
    """
    raw = data.get("completed_at")
    if raw:
        return raw
    if status == "completed":
        return data.get("updated_at")
    return None


def _values_from_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    """The attempt-shaped columns a run or attempt payload carries.

    Both shapes work: ``GET /runs/{id}`` returns the *latest* attempt's values
    under ``run_attempt`` equal to that attempt, and
    ``GET /runs/{id}/attempts/{n}`` returns that attempt's own. Reading them the
    same way is what lets either source upsert this table.
    """
    from .states import normalize_conclusion, normalize_status, parse_timestamp

    status = normalize_status(data.get("status"))
    return {
        "status": status,
        "conclusion": normalize_conclusion(data.get("conclusion")),
        "actor": _actor_login(data),
        "started_at": parse_timestamp(data.get("run_started_at") or data.get("started_at")),
        "completed_at": parse_timestamp(_completed_at(data, status)),
        "github_created_at": parse_timestamp(data.get("created_at")),
        "github_updated_at": parse_timestamp(data.get("updated_at")),
    }


def _merge(record: Any, values: dict[str, Any]) -> bool:
    """Apply a payload's values to an attempt row, refusing to move backwards.

    ``status``/``conclusion`` go through the forward-only state machine;
    timestamps and metadata are filled in when the payload carries them, and
    ``completed_at`` only when the execution is (now) completed -- a late
    ``in_progress`` payload must not clear it.
    """
    from .errors import InvalidStateTransitionError
    from .states import KIND_RUN, STATUS_COMPLETED, StateMachine

    before = (record.status, record.conclusion)
    changed = False
    try:
        changed = StateMachine.apply_transition(
            record, values.get("status"), values.get("conclusion"), KIND_RUN
        )
    except InvalidStateTransitionError as exc:
        logger.warning(
            "Ignoring out-of-order status for attempt %s of run %s: %s",
            record.attempt_number,
            record.run_id,
            exc,
        )

    # A same-status replay refreshes the conclusion without changing the
    # status, so the machine's return value alone does not say "saved".
    if (record.status, record.conclusion) != before:
        changed = True

    for name, value in values.items():
        if name in ("status", "conclusion") or value is None:
            continue
        if name == "completed_at" and record.status != STATUS_COMPLETED:
            continue
        if getattr(record, name) != value:
            setattr(record, name, value)
            changed = True
    return changed


def _advance(record: Any, values: dict[str, Any]) -> Any:
    """Merge in the sync twin and save when anything moved."""
    if _merge(record, values):
        record.save()
    return record


async def _advance_async(record: Any, values: dict[str, Any]) -> Any:
    """Merge in the async twin and save when anything moved."""
    if _merge(record, values):
        await record.save()
    return record


class RunAttempt(DefaultTimestampMixin, ActiveRecord):
    """One execution of a workflow run, synchronous twin."""

    __table_name__ = "ct_run_attempts"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    #: Local run this attempt belongs to. The run id is *reused* across
    #: attempts: only :attr:`attempt_number` tells the executions apart.
    run_id: int = 0
    attempt_number: int = 1
    status: str = "queued"
    conclusion: str | None = None
    #: Who triggered this attempt. A re-run can be started by someone other than
    #: the run's original author, so this is not the run's actor.
    actor: str | None = None
    #: GitHub's ``run_started_at`` for *this* attempt.
    started_at: datetime | None = None
    #: GitHub's ``completed_at`` for *this* attempt; ``None`` while it runs.
    completed_at: datetime | None = None
    #: GitHub's ``completed_at`` for *this* attempt; ``None`` while it runs.
    completed_at: datetime | None = None
    #: GitHub's ``created_at`` / ``updated_at`` for this attempt, prefixed so
    #: they cannot be mistaken for this row's own timestamps.
    github_created_at: datetime | None = None
    github_updated_at: datetime | None = None

    #: Owning run (pairs with :attr:`WorkflowRun.attempts`).
    run: ClassVar[BelongsTo[WorkflowRun]] = BelongsTo(foreign_key="run_id", inverse_of="attempts")
    #: Jobs of this attempt, in local id order.
    jobs: ClassVar[OrderedHasMany[Job]] = OrderedHasMany(
        foreign_key="attempt_id",
        inverse_of="attempt",
        order_key=lambda job: job.id,
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    def for_run(cls, run_id: int) -> ActiveQuery:
        """Query builder over every stored attempt of one run, number ascending."""
        return cls.query().where(cls.c.run_id == run_id).order_by((cls.c.attempt_number, "ASC"))

    @classmethod
    def find_attempt(cls, run_id: int, attempt_number: int) -> RunAttempt | None:
        """Return one stored attempt by its natural key, or ``None``."""
        return cls.find_one((cls.c.run_id == run_id) & (cls.c.attempt_number == attempt_number))

    @classmethod
    def latest_for_run(cls, run_id: int) -> RunAttempt | None:
        """Return the highest stored attempt number for a run, or ``None``.

        The run row's own ``run_attempt`` says the same thing, but answering it
        from this table keeps "what do we know about this run's executions"
        in one place -- and it stays right when a re-run arrives before the run
        row is refreshed.

        The query is built here rather than by chaining onto :meth:`for_run`:
        that one orders ascending, and the ORM appends a second ``order_by``
        instead of replacing the first, so the descending key was decoration and
        the answer was always the *earliest* attempt.
        """
        return (
            cls.query()
            .where(cls.c.run_id == run_id)
            .order_by((cls.c.attempt_number, "DESC"))
            .limit(1)
            .one()
        )

    # ---- behavior -------------------------------------------------------------

    @property
    def is_finished(self) -> bool:
        """Whether this execution has reached a terminal state."""
        from .states import STATUS_COMPLETED

        return self.status == STATUS_COMPLETED

    @classmethod
    def ensure(cls, run: WorkflowRun, attempt_number: int) -> RunAttempt:
        """Return the attempt row for ``(run, attempt_number)``, creating it if absent.

        The foreign key on ``ct_jobs`` is NOT NULL, so a job cannot be stored
        before its attempt exists. A ``workflow_job`` webhook fragment can carry
        a job whose attempt has never been stored -- the fragment names the
        attempt but says nothing about its status -- so the write path asks for
        the row rather than assuming it. A minimal row is created and the fuller
        one overwrites it later, via :meth:`upsert_from_remote`.
        """
        record = cls.find_attempt(run.id or 0, attempt_number)
        if record is not None:
            return record
        record = cls(
            run_id=run.id or 0,
            attempt_number=attempt_number,
            status="queued",
        )
        record.save()
        logger.debug(
            "Created run %s attempt %s from a fragment",
            run.github_run_id,
            attempt_number,
        )
        return record

    @classmethod
    def upsert_from_remote(cls, run: WorkflowRun, data: Mapping[str, Any]) -> RunAttempt:
        """Create or refresh the attempt row a run/attempt payload describes.

        Keyed by ``(run_id, attempt_number)``. A stored attempt is refreshed in
        place; an unseen one is inserted, which is what makes attempt 1's state
        survive a re-run instead of being overwritten by it.

        Args:
            run: The persisted run this attempt belongs to.
            data: A run or attempt payload. The payload's own ``run_attempt``
                wins; the run's is the fallback for fragments carrying none.

        Returns:
            The persisted :class:`RunAttempt` record.
        """
        latest = cls.latest_for_run(run.id or 0)
        attempt_number = _as_int(data.get("run_attempt")) or (
            latest.attempt_number if latest else 1
        )
        values = _values_from_payload(data)

        record = cls.find_attempt(run.id or 0, attempt_number)
        if record is None:
            record = cls(run_id=run.id or 0, attempt_number=attempt_number, **values)
            record.save()
            return record

        # One attempt is one execution: its state moves forward only. A late
        # ``in_progress`` for an attempt that already completed is the past
        # arriving out of order and is skipped, never allowed to rewrite it.
        _advance(record, values)
        return record


class AsyncRunAttempt(DefaultTimestampMixin, AsyncActiveRecord):
    """One execution of a workflow run, asynchronous twin."""

    __table_name__ = "ct_run_attempts"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    run_id: int = 0
    attempt_number: int = 1
    status: str = "queued"
    conclusion: str | None = None
    actor: str | None = None
    started_at: datetime | None = None
    #: GitHub's ``completed_at`` for *this* attempt; ``None`` while it runs.
    completed_at: datetime | None = None
    github_created_at: datetime | None = None
    github_updated_at: datetime | None = None

    #: Owning run (pairs with :attr:`AsyncWorkflowRun.attempts`).
    run: ClassVar[AsyncBelongsTo[AsyncWorkflowRun]] = AsyncBelongsTo(
        foreign_key="run_id", inverse_of="attempts"
    )
    #: Jobs of this attempt, in local id order.
    jobs: ClassVar[AsyncOrderedHasMany[AsyncJob]] = AsyncOrderedHasMany(
        foreign_key="attempt_id",
        inverse_of="attempt",
        order_key=lambda job: job.id,
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    def for_run(cls, run_id: int) -> AsyncActiveQuery:
        """Query builder over every stored attempt of one run, number ascending."""
        return cls.query().where(cls.c.run_id == run_id).order_by((cls.c.attempt_number, "ASC"))

    @classmethod
    async def find_attempt(cls, run_id: int, attempt_number: int) -> AsyncRunAttempt | None:
        """Return one stored attempt by its natural key, or ``None``."""
        return await cls.find_one(
            (cls.c.run_id == run_id) & (cls.c.attempt_number == attempt_number)
        )

    @classmethod
    async def latest_for_run(cls, run_id: int) -> AsyncRunAttempt | None:
        """Return the highest stored attempt number for a run, or ``None``.

        See :meth:`RunAttempt.latest_for_run`: building the query here keeps the
        descending order as the only key, instead of appending it to
        :meth:`for_run`'s ascending one.
        """
        return await (
            cls.query()
            .where(cls.c.run_id == run_id)
            .order_by((cls.c.attempt_number, "DESC"))
            .limit(1)
            .one()
        )

    # ---- behavior -------------------------------------------------------------

    @property
    def is_finished(self) -> bool:
        """Whether this execution has reached a terminal state.

        See :attr:`RunAttempt.is_finished`; the same predicate.
        """
        from .states import STATUS_COMPLETED

        return self.status == STATUS_COMPLETED

    @classmethod
    async def ensure(cls, run: AsyncWorkflowRun, attempt_number: int) -> AsyncRunAttempt:
        """Return the attempt row for ``(run, attempt_number)``, creating it if absent.

        See :meth:`RunAttempt.ensure`; the async twin, same behaviour.
        """
        record = await cls.find_attempt(run.id or 0, attempt_number)
        if record is not None:
            return record
        record = cls(
            run_id=run.id or 0,
            attempt_number=attempt_number,
            status="queued",
        )
        await record.save()
        logger.debug(
            "Created run %s attempt %s from a fragment",
            run.github_run_id,
            attempt_number,
        )
        return record

    @classmethod
    async def upsert_from_remote(
        cls, run: AsyncWorkflowRun, data: Mapping[str, Any]
    ) -> AsyncRunAttempt:
        """Create or refresh the attempt row a run/attempt payload describes.

        See :meth:`RunAttempt.upsert_from_remote`; the async twin.
        """
        latest = await cls.latest_for_run(run.id or 0)
        attempt_number = _as_int(data.get("run_attempt")) or (
            latest.attempt_number if latest else 1
        )
        values = _values_from_payload(data)

        record = await cls.find_attempt(run.id or 0, attempt_number)
        if record is None:
            record = cls(run_id=run.id or 0, attempt_number=attempt_number, **values)
            await record.save()
            return record

        # See :meth:`RunAttempt.upsert_from_remote`: forward-only, per attempt.
        await _advance_async(record, values)
        return record


__all__ = ["AsyncRunAttempt", "RunAttempt"]
