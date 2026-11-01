# src/rhosocial/github_ci_tracker/models/step.py
"""Workflow job step model, sync and async twins.

A step is one phase of a job's execution as GitHub reports it in the job
payload (``number``, ``name``, ``status``, ``conclusion`` and the two
timestamps). GitHub does not give steps a stable id of their own, so the
``github_step_id`` column mirrors the step ``number`` -- the same convention
the schema's ``UNIQUE(job_id, github_step_id)`` constraint is built on.

Steps are stored exclusively through :meth:`Step.upsert_from_remote`, keyed
by ``(job_id, number)``; it is called by
:meth:`Job.upsert_from_remote
<rhosocial.github_ci_tracker.models.job.Job.upsert_from_remote>` for every
entry of a job payload's ``steps`` array.

The twins are behavior-identical; the sync one exists for the project's
sync/async parity rule. There are no cross-model references here -- the
methods scope themselves by ``job_id`` -- so this module needs no lazy
imports beyond the state machine used by the upsert.
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

if TYPE_CHECKING:
    from .job import AsyncJob, Job
    from .log import AsyncStepLog, StepLog

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


class Step(DefaultTimestampMixin, ActiveRecord):
    """One step of a job, synchronous twin."""

    __table_name__ = "ct_steps"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    #: Local job this step belongs to.
    job_id: int = 0
    #: Mirrors the step ``number``: GitHub step payloads carry no separate
    #: id, and the schema's ``UNIQUE(job_id, github_step_id)`` constraint is
    #: aligned with the ``(job_id, number)`` upsert key.
    github_step_id: int = 0
    name: str = ""
    status: str = "queued"
    conclusion: str | None = None
    number: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None

    #: Owning job (pairs with :attr:`Job.steps`).
    job: ClassVar[BelongsTo[Job]] = BelongsTo(foreign_key="job_id", inverse_of="steps")

    #: What this step printed, when it was captured. Optional: GitHub keeps the
    #: per-step archive for about an hour after the run finishes, so an unread
    #: run has no row here and cannot get one later -- absence is the answer,
    #: not a failure.
    step_log: ClassVar[HasOne[StepLog]] = HasOne(foreign_key="step_id", inverse_of="step")

    # ---- query domain -------------------------------------------------------

    @classmethod
    def for_job(cls, job_id: int) -> ActiveQuery:
        """Query builder over the steps of one job, number ascending.

        The builder is not terminated; callers may add further clauses
        before calling ``all()`` / ``one()`` / ``count()``.
        """
        return cls.query().where(cls.c.job_id == job_id).order_by((cls.c.number, "ASC"))

    # ---- behavior -------------------------------------------------------------

    def upsert_from_remote(self, step_data: Mapping[str, Any]) -> Step:
        """Create or update this job's step from a remote step payload.

        The lookup is keyed by ``(job_id, number)`` -- the receiving
        instance supplies the job, the payload supplies the number:

        1. a stored step with that number moves through the forward-only
           state machine (a stale or out-of-order payload is logged and
           skipped, never fatal) and is refreshed in place (name,
           conclusion, timestamps);
        2. otherwise the payload is stored as a new row -- the receiving
           instance itself when it is not persisted yet, a fresh row when
           it is. ``github_step_id`` is set to the number, keeping the
           column aligned with the lookup key.

        Args:
            step_data: One entry of a GitHub job payload's ``steps`` array.

        Returns:
            The persisted :class:`Step` record.
        """
        from .errors import InvalidStateTransitionError
        from .states import (
            KIND_STEP,
            StateMachine,
            normalize_conclusion,
            normalize_status,
            parse_timestamp,
        )

        cls = type(self)

        number = _as_int(step_data.get("number")) or 0
        status = normalize_status(step_data.get("status"))
        conclusion = normalize_conclusion(step_data.get("conclusion"))
        started_at = parse_timestamp(step_data.get("started_at"))
        completed_at = parse_timestamp(step_data.get("completed_at"))
        name = _clean_str(step_data.get("name"))

        existing = cls.find_one((cls.c.job_id == self.job_id) & (cls.c.number == number))
        if existing is not None:
            try:
                StateMachine.apply_transition(existing, status, conclusion, KIND_STEP)
            except InvalidStateTransitionError as exc:
                logger.warning("Ignoring out-of-order status for step %s: %s", number, exc)
            if name:
                existing.name = name
            if started_at is not None:
                existing.started_at = started_at
            if completed_at is not None:
                existing.completed_at = completed_at
            if existing.is_dirty:
                existing.save()
            return existing

        record = self if self.id is None else cls(job_id=self.job_id)
        record.job_id = self.job_id
        record.github_step_id = number
        record.number = number
        record.name = name
        record.status = status
        record.conclusion = conclusion
        record.started_at = started_at
        record.completed_at = completed_at
        try:
            record.save()
        except IntegrityError:
            # A poll and a delivery (or two polls) both read "no row" and both
            # inserted; the unique key rejects the loser. The winner is the row
            # to answer with -- failing here used to abort the whole refresh, so
            # one concurrent writer made the read path return an error.
            winner = cls.find_one((cls.c.job_id == self.job_id) & (cls.c.number == number))
            if winner is None:
                raise
            return winner
        logger.debug("Stored step %s ('%s') for job %s", number, name, self.job_id)
        return record


class AsyncStep(DefaultTimestampMixin, AsyncActiveRecord):
    """One step of a job, asynchronous twin.

    Behavior-identical to :class:`Step`; every method performs the same
    database operations and raises the same exceptions.
    """

    __table_name__ = "ct_steps"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    #: Local job this step belongs to.
    job_id: int = 0
    #: Mirrors the step ``number``: GitHub step payloads carry no separate
    #: id, and the schema's ``UNIQUE(job_id, github_step_id)`` constraint is
    #: aligned with the ``(job_id, number)`` upsert key.
    github_step_id: int = 0
    name: str = ""
    status: str = "queued"
    conclusion: str | None = None
    number: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None

    #: Owning job (pairs with :attr:`AsyncJob.steps`).
    job: ClassVar[AsyncBelongsTo[AsyncJob]] = AsyncBelongsTo(
        foreign_key="job_id", inverse_of="steps"
    )

    #: What this step printed, when it was captured (see the sync twin).
    step_log: ClassVar[AsyncHasOne[AsyncStepLog]] = AsyncHasOne(
        foreign_key="step_id", inverse_of="step"
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    def for_job(cls, job_id: int) -> AsyncActiveQuery:
        """Query builder over the steps of one job, number ascending.

        See :meth:`Step.for_job`; the builder is not terminated.
        """
        return cls.query().where(cls.c.job_id == job_id).order_by((cls.c.number, "ASC"))

    # ---- behavior -------------------------------------------------------------

    async def upsert_from_remote(self, step_data: Mapping[str, Any]) -> AsyncStep:
        """Create or update this job's step from a remote step payload.

        See :meth:`Step.upsert_from_remote` for the ``(job_id, number)``
        key resolution and the state machine semantics; the stored field
        semantics are identical.
        """
        from .errors import InvalidStateTransitionError
        from .states import (
            KIND_STEP,
            StateMachine,
            normalize_conclusion,
            normalize_status,
            parse_timestamp,
        )

        cls = type(self)

        number = _as_int(step_data.get("number")) or 0
        status = normalize_status(step_data.get("status"))
        conclusion = normalize_conclusion(step_data.get("conclusion"))
        started_at = parse_timestamp(step_data.get("started_at"))
        completed_at = parse_timestamp(step_data.get("completed_at"))
        name = _clean_str(step_data.get("name"))

        existing = await cls.find_one((cls.c.job_id == self.job_id) & (cls.c.number == number))
        if existing is not None:
            try:
                StateMachine.apply_transition(existing, status, conclusion, KIND_STEP)
            except InvalidStateTransitionError as exc:
                logger.warning("Ignoring out-of-order status for step %s: %s", number, exc)
            if name:
                existing.name = name
            if started_at is not None:
                existing.started_at = started_at
            if completed_at is not None:
                existing.completed_at = completed_at
            if existing.is_dirty:
                await existing.save()
            return existing

        record = self if self.id is None else cls(job_id=self.job_id)
        record.job_id = self.job_id
        record.github_step_id = number
        record.number = number
        record.name = name
        record.status = status
        record.conclusion = conclusion
        record.started_at = started_at
        record.completed_at = completed_at
        try:
            await record.save()
        except IntegrityError:
            # See :meth:`Step.upsert_from_remote`: the loser of an insert race
            # adopts the winner instead of failing the caller's refresh.
            winner = await cls.find_one((cls.c.job_id == self.job_id) & (cls.c.number == number))
            if winner is None:
                raise
            return winner
        logger.debug("Stored step %s ('%s') for job %s", number, name, self.job_id)
        return record
