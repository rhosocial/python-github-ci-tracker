# src/rhosocial/github_ci_tracker/models/log.py
"""Log models, sync and async twins: one per step, and one per job.

Two tables, because GitHub hands out two different things:

* :class:`StepLog` -- one row per **step** (``ct_step_logs``). This is the
  preferred unit and the only one that can say "this is what step N printed".
  Its source is the per-step file inside a run's log archive
  (``<job>/<number>_<name>.txt``).
* :class:`JobLog` -- one row per **job** (``ct_job_logs``), the merged text.
  It is the fallback: a run whose archive no longer carries per-step files
  (GitHub keeps them for roughly an hour after the run finishes, then rewrites
  the archive flat), a run whose capture failed, or a caller that asked for the
  whole job at once.

Either way the association is one-to-one and the optional side is the **absence
of a row**, not a nullable pointer on the parent: deleting a log leaves its step
or job untouched and needs no UPDATE, while deleting the parent still takes its
log with it. A log can therefore be purged on its own -- by age, by repository,
by "nobody will ever read this again" -- without touching a single parent row.

**A step log is not guaranteed to exist.** The archive loses its per-step files
about an hour after the run completes, so a step logged later than that has no
row and never will; ``None`` from :meth:`StepLog.find_for_step` is a real
answer, not a miss to be retried forever. Callers fall back to the job log.

**Nothing here is chunked.** An earlier version split each document into
character rows because "single rows should stay small", an assumption nobody
measured. Measured against the largest log this service holds (3.4MB, 20819
lines), it cost 1489ms in 33 sequential inserts and bought nothing: the read
path concatenated every chunk anyway. One row costs 223ms and PostgreSQL keeps
376KB of it on disk, TOASTed and compressed.

The twins are behavior-identical; the sync one exists for the project's
sync/async parity rule. The ``core`` package is imported lazily inside method
bodies, because its ``__init__`` imports the event handlers, which import the
models.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, ClassVar

from rhosocial.activerecord.base.field_proxy import FieldProxy
from rhosocial.activerecord.field.timestamp import DefaultTimestampMixin
from rhosocial.activerecord.model import ActiveRecord, AsyncActiveRecord
from rhosocial.activerecord.query import ActiveQuery, AsyncActiveQuery
from rhosocial.activerecord.relation.async_descriptors import AsyncBelongsTo
from rhosocial.activerecord.relation.descriptors import BelongsTo

if TYPE_CHECKING:
    from .job import AsyncJob, Job
    from .step import AsyncStep, Step

logger = logging.getLogger(__name__)


class JobLog(DefaultTimestampMixin, ActiveRecord):
    """The whole log text of one job, synchronous twin."""

    __table_name__ = "ct_job_logs"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    #: Owning job. UNIQUE, so this is one-to-one: a job has at most one log row.
    job_id: int = 0
    log_content: str = ""
    #: Lines in :attr:`log_content`, counted at write time. Reading the text to
    #: count is exactly what a windowed read is trying to avoid.
    line_count: int = 0
    #: When GitHub last served this text.
    fetched_at: datetime | None = None

    #: Owning job (pairs with :attr:`Job.job_log`).
    job: ClassVar[BelongsTo[Job]] = BelongsTo(foreign_key="job_id", inverse_of="job_log")

    # ---- query domain -------------------------------------------------------

    @classmethod
    def find_for_job(cls, job_id: int) -> JobLog | None:
        """Return the stored log of a job, or ``None`` when it has none.

        ``None`` is a real answer -- the log was never fetched, or it was purged
        -- and callers that fetch-or-store depend on telling it apart from an
        empty log.
        """
        return cls.find_one(cls.c.job_id == job_id)

    @classmethod
    def for_job(cls, job_id: int) -> ActiveQuery:
        """Query builder over the log rows of one job.

        A builder, not a result, so a caller can add its own ordering before
        ``all()``. At most one row can match -- the column is UNIQUE.
        """
        return cls.query().where(cls.c.job_id == job_id)

    # ---- behavior -------------------------------------------------------------

    def store(self) -> None:
        """Persist this log, replacing any log already stored for the job."""
        existing = JobLog.find_for_job(self.job_id)
        if existing is None:
            self.id = None
            self.line_count = _count_lines(self.log_content)
            self.fetched_at = _now()
            self.save()
            return
        existing.log_content = self.log_content
        existing.line_count = _count_lines(self.log_content)
        existing.fetched_at = _now()
        existing.save()

    def lines(self) -> list[str]:
        """The stored text as lines, without inventing a trailing empty one."""
        return _split_lines(self.log_content)


class AsyncJobLog(DefaultTimestampMixin, AsyncActiveRecord):
    """The whole log text of one job, asynchronous twin."""

    __table_name__ = "ct_job_logs"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    job_id: int = 0
    log_content: str = ""
    line_count: int = 0
    fetched_at: datetime | None = None

    #: Owning job (pairs with :attr:`AsyncJob.job_log`).
    job: ClassVar[AsyncBelongsTo[AsyncJob]] = AsyncBelongsTo(
        foreign_key="job_id", inverse_of="job_log"
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    async def find_for_job(cls, job_id: int) -> AsyncJobLog | None:
        """Return the stored log of a job, or ``None`` when it has none."""
        return await cls.find_one(cls.c.job_id == job_id)

    @classmethod
    def for_job(cls, job_id: int) -> AsyncActiveQuery:
        """Query builder over the log rows of one job (at most one matches)."""
        return cls.query().where(cls.c.job_id == job_id)

    # ---- behavior -------------------------------------------------------------

    async def store(self) -> None:
        """Persist this log, replacing any log already stored for the job."""
        existing = await AsyncJobLog.find_for_job(self.job_id)
        if existing is None:
            self.id = None
            self.line_count = _count_lines(self.log_content)
            self.fetched_at = _now()
            await self.save()
            return
        existing.log_content = self.log_content
        existing.line_count = _count_lines(self.log_content)
        existing.fetched_at = _now()
        await existing.save()

    async def lines(self) -> list[str]:
        """The stored text as lines, without inventing a trailing empty one."""
        return _split_lines(self.log_content)


class StepLog(DefaultTimestampMixin, ActiveRecord):
    """What one step printed, synchronous twin.

    One row per step at most. Its absence is the normal case for any step whose
    run finished more than about an hour ago: GitHub rewrites the archive flat
    after that, so the per-step file is gone and cannot be recovered.
    """

    __table_name__ = "ct_step_logs"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    #: Owning step. UNIQUE, so this is one-to-one: a step has at most one log.
    step_id: int = 0
    log_content: str = ""
    #: Lines in :attr:`log_content`, counted at write time. Reading the text to
    #: count is exactly what a windowed read is trying to avoid.
    line_count: int = 0
    #: When GitHub last served this file. Steps of one run are captured in one
    #: request, so equal values across a run mean "one archive read".
    fetched_at: datetime | None = None

    #: Owning step (pairs with :attr:`Step.step_log`).
    step: ClassVar[BelongsTo[Step]] = BelongsTo(foreign_key="step_id", inverse_of="step_log")

    # ---- query domain -------------------------------------------------------

    @classmethod
    def find_for_step(cls, step_id: int) -> StepLog | None:
        """Return the stored log of a step, or ``None`` when it has none.

        ``None`` is a real answer and the common one for older runs: the file
        existed for about an hour and is gone now. Callers that need text fall
        back to the job log rather than retrying.
        """
        return cls.find_one(cls.c.step_id == step_id)

    @classmethod
    def for_step(cls, step_id: int) -> ActiveQuery:
        """Query builder over the log rows of one step (at most one matches)."""
        return cls.query().where(cls.c.step_id == step_id)

    @classmethod
    def stored_count_for_steps(cls, step_ids: list[int]) -> int:
        """How many of ``step_ids`` already have a log row.

        One query, used by the capture path to answer "did this run's logs land
        at all", without reading a single document.
        """
        if not step_ids:
            return 0
        return cls.query().where(cls.c.step_id.in_(step_ids)).count()

    # ---- behavior -------------------------------------------------------------

    def store(self) -> None:
        """Persist this log, replacing any log already stored for the step."""
        existing = StepLog.find_for_step(self.step_id)
        if existing is None:
            self.id = None
            self.line_count = _count_lines(self.log_content)
            self.fetched_at = _now()
            self.save()
            return
        existing.log_content = self.log_content
        existing.line_count = _count_lines(self.log_content)
        existing.fetched_at = _now()
        existing.save()

    def lines(self) -> list[str]:
        """The stored text as lines, without inventing a trailing empty one."""
        return _split_lines(self.log_content)


class AsyncStepLog(DefaultTimestampMixin, AsyncActiveRecord):
    """What one step printed, asynchronous twin."""

    __table_name__ = "ct_step_logs"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    step_id: int = 0
    log_content: str = ""
    line_count: int = 0
    fetched_at: datetime | None = None

    #: Owning step (pairs with :attr:`AsyncStep.step_log`).
    step: ClassVar[AsyncBelongsTo[AsyncStep]] = AsyncBelongsTo(
        foreign_key="step_id", inverse_of="step_log"
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    async def find_for_step(cls, step_id: int) -> AsyncStepLog | None:
        """Return the stored log of a step, or ``None`` when it has none."""
        return await cls.find_one(cls.c.step_id == step_id)

    @classmethod
    def for_step(cls, step_id: int) -> AsyncActiveQuery:
        """Query builder over the log rows of one step (at most one matches)."""
        return cls.query().where(cls.c.step_id == step_id)

    @classmethod
    async def stored_count_for_steps(cls, step_ids: list[int]) -> int:
        """How many of ``step_ids`` already have a log row (one query)."""
        if not step_ids:
            return 0
        return await cls.query().where(cls.c.step_id.in_(step_ids)).count()

    # ---- behavior -------------------------------------------------------------

    async def store(self) -> None:
        """Persist this log, replacing any log already stored for the step."""
        existing = await AsyncStepLog.find_for_step(self.step_id)
        if existing is None:
            self.id = None
            self.line_count = _count_lines(self.log_content)
            self.fetched_at = _now()
            await self.save()
            return
        existing.log_content = self.log_content
        existing.line_count = _count_lines(self.log_content)
        existing.fetched_at = _now()
        await existing.save()

    async def lines(self) -> list[str]:
        """The stored text as lines, without inventing a trailing empty one."""
        return _split_lines(self.log_content)


# ---- shared internals -----------------------------------------------------


def _count_lines(content: str) -> int:
    """Lines in ``content``; an empty document has zero, not one."""
    if not content:
        return 0
    return content.count("\n") + (0 if content.endswith("\n") else 1)


def _split_lines(content: str) -> list[str]:
    """Split without inventing a trailing empty line, and strip a leading BOM.

    GitHub's job logs begin with a BOM. Leaving it on the first line puts an
    invisible character in front of text the caller is asked to read.
    """
    if not content:
        return []
    return content.lstrip("\ufeff").splitlines()


def _now() -> datetime:
    """Current UTC time, as the timestamp field type expects."""
    return datetime.now(timezone.utc)


__all__ = ["AsyncJobLog", "AsyncStepLog", "JobLog", "StepLog"]
