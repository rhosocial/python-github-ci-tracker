# src/rhosocial/github_ci_tracker/controllers/logs.py
"""Capturing step logs while GitHub still has them, and reading them back.

GitHub serves a run's archive as one file per step only for about an hour after
the run finishes; after that the same endpoint answers with one merged file per
job and the step boundaries are gone for good (measured, see
``.claude/plan/2026-10-31/step-log-investigation.md``). **That deadline is
GitHub's, and it is a deadline for fetching, not for reading.** Everything
fetched becomes an ordinary row, kept as long as its job is: nothing here
expires, and no read path consults the window.

So the flow here is a *capture*, not a read:

* the delivery channel calls :meth:`RunLogCapture.ensure` when a run completes,
  which is the earliest and safest moment;
* a read while the deadline is still open calls it too, so a missed delivery or
  a run created outside this service still gets its chance;
* a read after the deadline writes nothing and says so, and the caller falls
  back to the job log -- which is fetched by the unchanged single-job path.

Nothing here is atomic and nothing here retries in the background: each step log
is its own row, a failed capture leaves what it managed to store, and the next
read sees it. That is the project's write model, not an accident.

The job log is **not** produced here. Its source stays
``GET /actions/jobs/{job_id}/logs``; the archive's merged file is byte-identical
to that body (measured), so the two paths never disagree about what a job
printed -- only about whether the step split was captured.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..config import LogSettings
from ..core.exceptions import CitrackerError
from ..services.github.protocols import RunReader
from ..views import CaptureStatus, LogCaptureView, StepSection
from .run import RunData

if TYPE_CHECKING:  # the rows these flows return; only the annotations name them
    from ..models import AsyncJob, AsyncRepository, AsyncStep, AsyncWorkflowRun

logger = logging.getLogger(__name__)

#: Why a capture stored nothing. Stable strings: adapters publish them.
SKIP_DISABLED = "capture_disabled"
SKIP_RUNNING = "run_not_finished"
SKIP_JOB_RUNNING = "job_not_finished"
SKIP_CLOSED = "capture_closed"
SKIP_ALREADY = "already_captured"
SKIP_NOT_STEP_ARCHIVE = "not_a_step_archive"
SKIP_NO_JOBS = "no_jobs_stored"


class RunLogCapture:
    """Turn a run's archive into step rows, and read those rows back.

    Args:
        readers: The GitHub read capability that serves the archive.
        runs_data: Run, job and step rows -- and their step logs.
        settings: The capture window and the on/off switch.
    """

    def __init__(
        self,
        *,
        readers: RunReader,
        runs_data: RunData,
        settings: LogSettings,
    ) -> None:
        self._readers = readers
        self._store = runs_data
        self._settings = settings

    # -- capture ---------------------------------------------------------------

    async def ensure(
        self,
        repository: AsyncRepository,
        run: AsyncWorkflowRun,
        *,
        job: AsyncJob | None = None,
        want: set[int] | None = None,
        retry: bool = False,
    ) -> LogCaptureView:
        """Capture step files, for one finished job or for a finished run.

        The unit is the **job**, because that is the unit GitHub finishes: a
        ``workflow_job`` delivery says one job's steps can no longer change, and
        its files are still in the archive at that moment. Waiting for the whole
        run would put a long matrix's earliest jobs at risk -- the archive keeps
        step files for roughly an hour, and a job that finished an hour before
        the run did may already be flat. The run-level call remains as the
        catch-all for runs whose job events were missed, and costs nothing when
        the jobs have already been captured.

        Safe to call on every read and on every completion: it answers "already
        captured" without touching the network, and it never raises for a
        GitHub failure -- the caller is usually a read path that has a job log
        to fall back on, and an exception there would turn "no step split" into
        "no logs at all".

        Args:
            repository: The subscribed repository the run belongs to.
            run: The run whose archive should be captured.
            job: Capture only this job's steps. Its own completion time decides
                whether the window is still open, so this works while the run is
                still going.
            want: Step numbers this call actually needs. Omitted, it is every
                step the job metadata says ran; a read that waits for its own
                step must not wait for the rest of the matrix.
            retry: Re-fetch while a wanted step still has no file. GitHub writes
                the per-step files asynchronously, so a job's steps can be final
                while its archive is still filling in -- and the response never
                says so. Only the read path asks for this; a webhook delivery
                must answer quickly.

        Returns:
            What happened, in a form both a log line and an API answer can use.
        """
        report = LogCaptureView(run_id=run.github_run_id)
        if not self._settings.capture_enabled:
            report.skipped_reason = SKIP_DISABLED
            return report

        if job is not None:
            if not job.is_finished or job.completed_at is None:
                report.skipped_reason = SKIP_JOB_RUNNING
                return report
            if _past_deadline(job.completed_at, self._settings.capture_window_minutes):
                report.skipped_reason = SKIP_CLOSED
                return report
            jobs: list[AsyncJob] = [job]
        else:
            attempt = await self._store.latest_attempt(run)
            finished = attempt.completed_at if attempt is not None else None
            if attempt is None or attempt.status != "completed" or finished is None:
                # Still running: the archive would be partial, and a partial
                # write would make "no step file" and "not finished yet"
                # indistinguishable later. The job events cover the rest.
                report.skipped_reason = SKIP_RUNNING
                return report
            if _past_deadline(finished, self._settings.capture_window_minutes):
                report.skipped_reason = SKIP_CLOSED
                return report
            jobs = await self._store.jobs(run)

        if not jobs:
            report.skipped_reason = SKIP_NO_JOBS
            return report

        steps_by_job: list[tuple[AsyncJob, dict[int, AsyncStep]]] = []
        captured: set[int] = set()
        expected: set[int] = set()
        for item in jobs:
            steps = await self._store.steps(item)
            if not steps:
                continue
            texts = await self._store.step_log_texts(steps)
            captured.update(texts)
            steps_by_job.append((item, {step.number: step for step in steps}))
            # What the job metadata says must have a file: a step that started
            # and was not skipped. Anything else may legitimately have none.
            expected.update(
                step.number
                for step in steps
                if step.started_at is not None and (step.conclusion or "") != "skipped"
            )

        if not steps_by_job:
            report.skipped_reason = SKIP_NO_JOBS
            return report

        wanted = set(want) if want is not None else expected
        if not wanted or wanted <= captured:
            # Everything this call needs is already stored: the archive is not
            # needed at all, which is what makes a repeated read free.
            report.skipped_reason = SKIP_ALREADY
            return report

        attempts = max(1, self._settings.capture_retry_attempts) if retry else 1
        delay = max(0.0, self._settings.capture_retry_delay_seconds)
        missing: set[int] = set()
        archive = None
        for attempt in range(1, attempts + 1):
            report.attempts = attempt
            try:
                archive = await self._readers.get_run_logs(repository.full_name, run.github_run_id)
            except CitrackerError as exc:
                status = getattr(exc, "status_code", None)
                report.error = f"{type(exc).__name__}: {exc}"
                # 404 until the run finishes, 5xx while GitHub's log store is
                # busy: both are "not ready", not "gone". A read that is allowed
                # to wait treats them that way; the last attempt still reports.
                if retry and status in (404, 502, 503) and attempt < attempts:
                    logger.info(
                        "Archive of run %s not ready (attempt %s/%s): %s",
                        run.github_run_id,
                        attempt,
                        attempts,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                report.skipped_reason = "archive_unavailable"
                logger.warning("Could not capture step logs of run %s: %s", run.github_run_id, exc)
                return report

            report.error = None  # an earlier attempt's 404 is not this attempt's
            report.form = archive.form
            for item, by_number in steps_by_job:
                files = archive.steps_for(item.name)
                if not files:
                    continue
                for number, step in by_number.items():
                    if number in captured:
                        continue
                    payload = files.get(number)
                    if payload is None:
                        continue
                    await self._store.store_step_log(step, payload.content)
                    captured.add(number)
                    report.steps_stored += 1

            missing = {number for number in wanted if number not in captured}
            if not missing or attempt == attempts:
                break
            # The files are written asynchronously after the job completes, so
            # a miss here means "not yet" far more often than "never". Wait and
            # ask again; the window still bounds how long this can go on.
            await asyncio.sleep(delay)

        matched = sum(
            1 for item, _ in steps_by_job if archive is not None and archive.knows_job(item.name)
        )
        report.jobs_matched = matched
        report.jobs_missing = len(steps_by_job) - matched
        report.steps_skipped = len(expected - captured)
        report.missing_steps = sorted(missing)
        if archive is not None and report.steps_stored == 0 and archive.form == "job":
            report.skipped_reason = SKIP_NOT_STEP_ARCHIVE
        elif missing:
            # Partial: some files arrived, some are still not there. The read
            # path asked, so it gets the honest gap rather than a silent one.
            report.skipped_reason = "partial_capture"
        logger.info(
            "Captured step logs of run %s: form=%s attempts=%s stored=%s missing=%s",
            run.github_run_id,
            archive.form if archive is not None else "-",
            report.attempts,
            report.steps_stored,
            report.missing_steps,
        )
        return report

    # -- read ------------------------------------------------------------------

    async def status(self, job: AsyncJob) -> CaptureStatus:
        """What this job's step-log capture currently holds.

        Stored rows only -- no GitHub call, no archive fetch. It exists so a
        caller can tell "these files have not arrived yet" from "these files
        never existed", which ``has_log: false`` alone cannot say.

        The two counts are log files, not steps: a skipped step, and a step that
        never started, have no file on GitHub either, so ``expected_logs`` is
        smaller than ``len(steps)`` and that difference is not a missing capture.
        """
        if not self._settings.capture_enabled:
            return CaptureStatus(state="disabled")
        steps = await self._store.steps(job)
        expected = [
            step
            for step in steps
            if step.started_at is not None and (step.conclusion or "") != "skipped"
        ]
        sizes = await self._store.step_log_sizes([step.id or 0 for step in expected])
        captured = sum(1 for step in expected if (step.id or 0) in sizes)
        if not expected:
            state = "none"
        elif captured == len(expected):
            state = "complete"
        elif captured:
            state = "partial"
        else:
            state = "none"
        closes = (
            job.completed_at + timedelta(minutes=self._settings.capture_window_minutes)
            if job.completed_at is not None
            else None
        )
        if (
            state != "complete"
            and job.completed_at is not None
            and _past_deadline(job.completed_at, self._settings.capture_window_minutes)
        ):
            # Nothing more can arrive: GitHub stopped serving this run's
            # per-step files. Saying "partial" forever would invite retries that
            # cannot succeed. What is already stored is untouched by this --
            # capture closing is about the source, never about the rows.
            state = "closed"
        return CaptureStatus(
            expected_logs=len(expected),
            captured_logs=captured,
            state=state,
            window_closes_at=closes,
        )

    async def sections(self, job: AsyncJob) -> list[StepSection]:
        """The stored step logs of one job, in step order.

        Steps without a capture are left out rather than returned empty: "this
        step printed nothing" and "this step's log was never captured" are
        different answers, and only the first one is a row.
        """
        steps = await self._store.steps(job)
        texts = await self._store.step_log_texts(steps)
        sections: list[StepSection] = []
        for step in steps:
            content = texts.get(step.number)
            if content is None:
                continue
            sections.append(
                StepSection(
                    number=step.number,
                    name=step.name,
                    conclusion=step.conclusion,
                    content=content,
                    line_count=_line_count(content),
                )
            )
        return sections

    async def job_is_captured(self, job: AsyncJob) -> bool:
        """Whether every step of this job has a stored log."""
        steps = await self._store.steps(job)
        if not steps:
            return False
        return await self._store.step_log_count(steps) >= len(steps)


def _past_deadline(finished: datetime, window_minutes: int) -> bool:
    """Whether GitHub has stopped serving per-step files for this run.

    The deadline is about the source, not about storage: rows captured before
    it stay readable forever, and this predicate is only ever asked before a
    fetch.
    """
    moment = finished if finished.tzinfo is not None else finished.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - moment > timedelta(minutes=window_minutes)


def _line_count(content: str) -> int:
    """Lines in ``content``; an empty document has zero, not one."""
    if not content:
        return 0
    return content.count("\n") + (0 if content.endswith("\n") else 1)


__all__ = [
    "RunLogCapture",
    "SKIP_ALREADY",
    "SKIP_DISABLED",
    "SKIP_JOB_RUNNING",
    "SKIP_CLOSED",
    "SKIP_NO_JOBS",
    "SKIP_NOT_STEP_ARCHIVE",
    "SKIP_RUNNING",
]
