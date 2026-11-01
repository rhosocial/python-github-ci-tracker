# src/rhosocial/github_ci_tracker/views.py
"""Protocol-shaped data: the objects a caller sees, and the pure conversions into them.

Nothing here awaits and nothing here queries: every function takes a row the
controller already returned and shapes it. A conversion that would need another
read belongs in the controller, which is where the read model lives now.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .core.exceptions import CitrackerError
from .models.states import FAILED_CONCLUSIONS

if TYPE_CHECKING:  # the views only read these; they never construct or query them
    from .models import AsyncJob, AsyncStep, AsyncWorkflowRun

#: Environment variable holding the webhook callback URL.
ENV_WEBHOOK_URL = "CITRACKER_WEBHOOK_URL"
#: Environment variable holding the webhook HMAC secret.
ENV_WEBHOOK_SECRET = "CITRACKER_WEBHOOK_SECRET"

#: ``RunRecord.cache_state``: every attempt GitHub reports is stored locally and
#: carries its jobs, and the run is finished, so the local row is the final
#: answer.
CACHE_STATE_CACHED = "cached"
#: Some of the run is missing locally -- an attempt, or the jobs of the newest
#: cached attempt. Asking for it will cost a network round trip.
CACHE_STATE_PARTIAL = "partial"
#: Nothing is missing, but the run is still running, so the local row may lag
#: GitHub until the next webhook or ``refresh``.
CACHE_STATE_STALE = "stale"


@dataclass(frozen=True)
class DurationSample:
    """How long a workflow's recent finished runs took, from the stored rows.

    A caller about to wait needs a budget, and the only honest source is what
    this workflow actually did lately. ``p90`` is what a budget should follow: a
    median-sized wait is exceeded half the time, which reads as "still running"
    exactly when the caller is least willing to keep waiting.
    """

    #: How many finished runs the figures are based on.
    samples: int
    p50_seconds: float
    p90_seconds: float


@dataclass
class RunView:
    """Result of a trigger or rerun request."""

    run_id: int = 0
    status: str = "queued"
    message: str = ""
    html_url: str = ""
    conclusion: str | None = None
    attempt: int | None = None
    #: The attempt the run was at before this request; ``None`` for a trigger,
    #: which starts a run rather than advancing one.
    previous_attempt: int | None = None
    #: ``False`` when ``if_absent`` found a run for the ref's head and returned
    #: it instead of spending CI minutes. A trigger that did not trigger has to
    #: say so, or the caller believes it started a run.
    dispatched: bool = True
    #: How long this workflow's recent runs took, so the first wait can be
    #: budgeted without another call. ``None`` when there is nothing to sample.
    duration_sample: DurationSample | None = None


@dataclass
class RunWaitView:
    """Result of waiting for a run to reach a terminal state."""

    run_id: int = 0
    status: str = ""
    conclusion: str | None = None
    message: str = ""
    completed: bool = False
    duration_seconds: float | None = None
    #: The same evidence, attached when the wait timed out, so the next call
    #: takes a measured timeout instead of repeating a guess. Left ``None`` on a
    #: completed wait: the run's own ``duration_seconds`` is the answer there,
    #: and sampling it would add two queries to the common fast path.
    duration_sample: DurationSample | None = None


def _run_url(repo_full_name: str, github_run_id: int) -> str:
    """Build the GitHub HTML URL of a run."""
    return f"https://github.com/{repo_full_name}/actions/runs/{github_run_id}"


@dataclass(frozen=True)
class JobFailure:
    """One job that did not pass, as a list reports it.

    A name and a conclusion: enough to answer "which job went red" for a batch
    of repositories. Steps and logs stay behind ``get_run``.
    """

    name: str = ""
    conclusion: str = ""


@dataclass
class RunRecord:
    """A workflow run as callers see it."""

    run_id: int = 0
    run_number: int = 0
    #: Attempt ordinal of this run; a re-run reuses the id and increments it.
    attempt: int = 1
    #: Whether ``attempt`` is the newest one -- ``False`` when the caller asked
    #: for a past execution, whose state is that attempt's, not the run's.
    is_latest: bool = True
    #: How many attempts GitHub reports for this run, as of the last sync.
    #: Never derived from the local attempt rows -- that is ``attempts_cached``.
    attempts_total: int = 1
    #: How many of those attempts are stored locally; ``0`` means none are.
    attempts_cached: int = 0
    #: Completeness of the local copy: one of :data:`CACHE_STATE_CACHED`,
    #: :data:`CACHE_STATE_PARTIAL`, :data:`CACHE_STATE_STALE`.
    cache_state: str = CACHE_STATE_PARTIAL
    status: str = "queued"
    conclusion: str | None = None
    branch: str = ""
    commit_sha: str = ""
    commit_message: str = ""
    #: GitHub's own one-line title for this run.
    display_title: str = ""
    actor: str = ""
    event: str = ""
    #: The workflow this run executes. Two runs of one commit are told apart by
    #: this and nothing else: ``test.yml`` and ``changelog-check.yml`` share a
    #: sha, a branch and an event.
    workflow_name: str = ""
    workflow_path: str = ""
    #: Cached pull request attribution (``None`` = unknown, e.g. fork PRs).
    pr_number: int | None = None
    #: Target branch of that pull request, when the run has one.
    base_ref: str = ""
    #: The commit the run executes: author name and commit time. The author is
    #: who wrote the code; ``actor`` is who triggered the run.
    commit_author: str = ""
    commit_time: datetime | None = None
    #: Repository the code came from; differs from the subscription on a fork.
    head_repo: str = ""
    #: When GitHub created the run -- the *remote* fact, which is what "how long
    #: ago" and any ordering means. Falls back to the attempt's start time for
    #: rows ingested before the column existed.
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    #: When this service first stored the row. Not a property of the run: it is
    #: the ingest time, useful for "how much did this process write recently",
    #: and misleading if read as the run's own age.
    stored_at: datetime | None = None
    html_url: str = ""
    jobs: list[JobRecord] = field(default_factory=list)
    #: ``None`` when the caller did not ask (a list does not pay for it);
    #: ``[]`` means the caller asked and every job passed.
    failed_jobs: list[JobFailure] | None = None

    @property
    def duration_seconds(self) -> float | None:
        """Elapsed seconds between start and completion."""
        if self.started_at is None:
            return None
        end = self.completed_at or datetime.now(self.started_at.tzinfo)
        return (end - self.started_at).total_seconds()


@dataclass
class RepositoryRuns:
    """One repository's slice of a batch answer.

    The error travels *inside* the entry rather than aborting the batch: a
    caller asking about fifteen repositories must still learn about the
    fourteen that answered.
    """

    repository: str
    runs: list[RunRecord] = field(default_factory=list)
    error: CitrackerError | None = None


@dataclass(frozen=True)
class CaptureStatus:
    """How much of a job's step logs were captured, and whether more can arrive.

    The window this describes is **GitHub's**, not this service's: GitHub serves
    a run's per-step files for about an hour after the run finishes, then only
    the merged document. Anything captured before that deadline is stored as an
    ordinary row and read back forever; the deadline only says whether *another
    attempt* could still add rows.

    Step files are written asynchronously after the job completes, so a
    ``has_log: false`` right after completion may mean "not yet" rather than
    "never". These numbers are the difference, and they come from stored rows
    only.
    """

    #: How many per-step log files this job *should* have: the steps that
    #: started and were not skipped. This is not ``len(steps)`` -- a skipped step,
    #: and one that never started, has no file on GitHub either, so counting them
    #: would promise logs that were never made.
    expected_logs: int = 0
    #: Of those, how many are stored here.
    captured_logs: int = 0
    #: ``complete`` / ``partial`` (some captured, more may still arrive) /
    #: ``none`` / ``disabled`` / ``closed`` (the source stopped serving step
    #: files; what was captured stays readable, nothing more can be added).
    state: str = "none"
    #: When GitHub stops serving per-step files for this job -- the end of the
    #: capture opportunity, not the end of readability. ``None`` when the job
    #: has not finished (nothing to close yet).
    window_closes_at: datetime | None = None


@dataclass
class JobRecord:
    """A job as callers see it."""

    job_id: int = 0
    run_id: int = 0
    name: str = ""
    status: str = "queued"
    conclusion: str | None = None
    runner_name: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    steps: list[StepRecord] = field(default_factory=list)
    #: Whether the conclusion says this job did not pass. ``None`` is not a
    #: failure -- GitHub leaves it unset while the row is being materialized.
    failed: bool = False
    #: How much of this job's step-log capture is here; ``None`` when the
    #: deployment has no capture flow.
    capture: CaptureStatus | None = None

    @property
    def duration_seconds(self) -> float | None:
        """Elapsed seconds between start and completion."""
        if self.started_at is None:
            return None
        end = self.completed_at or datetime.now(self.started_at.tzinfo)
        return (end - self.started_at).total_seconds()


@dataclass
class StepRecord:
    """A step as callers see it."""

    step_id: int = 0
    name: str = ""
    status: str = "queued"
    conclusion: str | None = None
    number: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    #: Whether a captured log exists for this step. False means the capture did
    #: not run while GitHub still served per-step files for this run -- not that
    #: the stored log went away: a captured log is readable forever, and the
    #: window only limits fetching.
    has_log: bool = False
    #: Lines in that log, when it exists. None means "no log", not "empty".
    log_lines: int | None = None
    #: Whether the conclusion says this step did not pass (see JobRecord.failed).
    failed: bool = False

    @property
    def duration_seconds(self) -> float | None:
        """Elapsed seconds between start and completion."""
        return _elapsed_seconds(self.started_at, self.completed_at)


@dataclass(frozen=True)
class StepSection:
    """One step's captured log, as a read returns it.

    ``name`` and ``conclusion`` come from the step row (the workflow's own
    ``name:``), never from the archive's file name -- that one is the rendered
    command and would not match what the caller sees on GitHub.
    """

    number: int = 0
    name: str = ""
    conclusion: str | None = None
    content: str = ""
    line_count: int = 0


@dataclass
class JobLogsView:
    """Log content of a job, from the step rows or from the merged document."""

    job_id: int = 0
    job_name: str = ""
    log_content: str = ""
    #: ``"step"`` when the text is the job's step rows (or one of them),
    #: ``"job"`` when it is the merged document served by the single-job
    #: endpoint. The two never mix in one answer.
    source: str = "job"
    #: Per-step sections, in step order, when the caller asked for them.
    sections: list[StepSection] = field(default_factory=list)
    #: Steps whose log was never captured. Their absence is an answer, not an
    #: error: the fetch did not happen while GitHub still split this run into
    #: per-step files (about an hour). Logs that *were* captured are not
    #: affected by that deadline -- they stay readable for as long as the row.
    steps_without_logs: list[int] = field(default_factory=list)
    #: Why ``sections`` is empty although step text was asked for.
    reason: str | None = None


@dataclass
class LogCaptureView:
    """Outcome of one attempt to capture a run's step files."""

    run_id: int = 0
    #: ``"step"`` or ``"job"`` -- which archive layout GitHub served.
    form: str = ""
    #: Jobs whose name matched a directory in the archive.
    jobs_matched: int = 0
    #: Jobs with steps but no step files in this archive.
    jobs_missing: int = 0
    steps_stored: int = 0
    steps_skipped: int = 0
    #: How many archive fetches this capture made. More than one means GitHub
    #: had not finished writing the step files when we first asked.
    attempts: int = 0
    #: Steps the job metadata says ran, but whose file never arrived.
    missing_steps: list[int] = field(default_factory=list)
    #: ``capture_disabled`` / ``run_not_finished`` / ``job_not_finished`` /
    #: ``capture_closed`` (GitHub stopped serving per-step files before we could
    #: fetch them) / ``already_captured`` / ``not_a_step_archive`` /
    #: ``no_jobs_stored`` / ``archive_unavailable`` -- and ``None`` when text was
    #: stored.
    skipped_reason: str | None = None
    error: str | None = None


@dataclass
class RunRefreshView:
    """Outcome of a forced run refresh (the pull channel)."""

    run_id: int = 0
    run_number: int = 0
    previous_status: str | None = None
    status: str = "queued"
    conclusion: str | None = None
    created: bool = False
    changed: bool = False
    jobs_synced: int = 0
    steps_synced: int = 0
    html_url: str = ""


#: ``JobHistoryView.verdict``: the job has not failed in any sampled execution.
JOB_VERDICT_NEVER = "never"
#: ``JobHistoryView.verdict``: every sampled execution of the job failed.
JOB_VERDICT_ALWAYS = "always"
#: ``JobHistoryView.verdict``: it failed in some sampled executions, not all.
JOB_VERDICT_INTERMITTENT = "intermittent"


@dataclass
class JobHistoryView:
    """How one job name has behaved across a repository's recent executions."""

    job_name: str = ""
    sampled_executions: int = 0
    failures: int = 0

    @property
    def verdict(self) -> str:
        """Which of the three answers this history supports.

        The question it serves is "re-run it, or fix the code": a job that
        failed in every sampled execution is not flaky, and one that never
        failed is not the cause of the failure being investigated. Rendering
        that into a sentence is the adapter's job, not this view's.
        """
        if self.failures == 0:
            return JOB_VERDICT_NEVER
        if self.sampled_executions > 1 and self.failures == self.sampled_executions:
            return JOB_VERDICT_ALWAYS
        return JOB_VERDICT_INTERMITTENT


@dataclass
class TokenRecord:
    """A stored credential as callers see it.

    The token value is deliberately absent: the service reads it to answer a
    probe and to build a request header, and never hands it back. A caller that
    needs the metadata -- an id to bind with, an owner to display, a measured
    capability -- gets it from here.
    """

    id: int = 0
    name: str = ""
    scope: str = ""
    owner_login: str | None = None
    is_active: bool = True
    expires_at: datetime | None = None
    can_read_actions: bool | None = None
    can_write_actions: bool | None = None
    can_manage_webhooks: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class BindingSummary:
    """How many subscriptions answer with a bound credential."""

    bound: int = 0
    #: Subscriptions with no binding. They read anonymously, which a public
    #: repository supports -- a fact to report, never a fault.
    anonymous: int = 0


#: ``StreamHealth.last_at`` never existed / the value is not known yet.
PUSH_CHANNEL_UNKNOWN = "unknown"
#: ``StatusSnapshot.push_channel``: a delivery arrived within the last hour.
PUSH_CHANNEL_ACTIVE = "active"
#: ``StatusSnapshot.push_channel``: the newest delivery is older than that.
PUSH_CHANNEL_STALE = "stale"


@dataclass
class StreamHealth:
    """One GitHub event stream, as the delivery ledger saw it.

    GitHub treats ``workflow_run`` and ``workflow_job`` as separate streams --
    separate payloads, separate lifecycles -- so each gets its own figures.
    """

    event_type: str = ""
    #: Arrival time of the newest delivery, or ``None`` when none ever came.
    last_at: datetime | None = None
    in_last_hour: int = 0
    deliveries_total: int = 0
    redelivered: int = 0
    unhandled: int = 0
    #: Why deliveries went unhandled, grouped by reason code.
    unhandled_reasons: dict[str, int] = field(default_factory=dict)
    #: Unhandled for a reason that is not a deliberate choice: our own failure.
    write_failures: int = 0
    #: The newest such row, as ``delivery_id``/``received_at``/``repository``/
    #: ``run_id``/``job_id``/``reason``.
    last_write_failure: dict[str, Any] | None = None
    #: Distinct run ids whose delivery failed and is still unhandled, newest
    #: first, capped by :data:`AFFECTED_RUNS_REPORTED`. A count says something is
    #: missing; these say which runs to re-read, so a repair is one pass instead
    #: of an investigation.
    affected_runs: list[int] = field(default_factory=list)


@dataclass
class StatusSnapshot:
    """What this service tracks, and whether the push channel is alive."""

    repository_count: int = 0
    runs_tracked: int = 0
    #: False when even the counts could not be read (an old or broken database).
    counts_available: bool = True
    #: False when the delivery ledger is missing, so health cannot be measured.
    ledger_available: bool = True
    error: str | None = None
    streams: dict[str, StreamHealth] = field(default_factory=dict)
    #: Promoted out of the reason histogram: ``{"code", "stream", "count",
    #: "last"}``. Rendering it is the adapter's job.
    alerts: list[dict[str, Any]] = field(default_factory=list)
    last_webhook_at: datetime | None = None
    webhooks_in_last_hour: int = 0
    push_channel: str = PUSH_CHANNEL_UNKNOWN
    #: How fast run rows are arriving: ``{"available", "runs_added_last_5m",
    #: "runs_added_last_hour", "backfill_suspected"}``. Separates "still
    #: ingesting history" from "the data is gone".
    ingest: dict[str, Any] = field(default_factory=dict)


def as_utc(value: datetime | None) -> datetime | None:
    """Normalize a timestamp to UTC, which is the only zone this service speaks.

    Datetimes reach a view from two sides: GitHub payloads, parsed from ISO
    strings that carry an offset, and database rows, which Postgres hands back in
    the connection's timezone. Rendering both as-is puts ``+08:00`` and
    ``+00:00`` in the same response, which reads as two different kinds of time.

    The result is always timezone-aware: this normalizes the offset, it never
    drops it. A caller comparing, sorting or rendering these values has the zone
    in hand -- it is UTC, stated, rather than implied.

    A naive value is taken as UTC: the columns are ``timestamptz`` (SQLite stores
    tests stores the same convention), and an offset the caller has to guess is
    worse than the one this service can state.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clean_str(value: Any, default: str = "") -> str:
    """Coerce a value to a stripped string."""
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _to_run_record(run: AsyncWorkflowRun, repo_full_name: str = "") -> RunRecord:
    """Convert a stored run into its public view.

    Only facts carried by the run row itself are filled in. The ``jobs`` field
    and the local-cache facts (``attempts_cached`` / ``cache_state``) need another
    read and are attached by :class:`~.query.QueryController`; until then the view
    honestly reports nothing cached.
    """
    return RunRecord(
        run_id=run.github_run_id,
        run_number=run.run_number,
        attempt=run.latest_attempt or 1,
        attempts_total=max(run.latest_attempt or 1, 1),
        status=run.latest_status or "queued",
        conclusion=run.latest_conclusion,
        branch=run.branch,
        commit_sha=run.commit_sha,
        commit_message=run.commit_message,
        display_title=getattr(run, "display_title", ""),
        head_repo=getattr(run, "head_repo", ""),
        commit_author=getattr(run, "commit_author", ""),
        commit_time=as_utc(getattr(run, "commit_time", None)),
        base_ref=getattr(run, "base_ref", ""),
        actor=run.actor,
        event=run.event,
        pr_number=run.pr_number,
        created_at=as_utc(getattr(run, "latest_github_created_at", None) or run.latest_started_at),
        started_at=as_utc(run.latest_started_at),
        completed_at=as_utc(run.latest_completed_at),
        stored_at=as_utc(run.created_at),
        html_url=_run_url(repo_full_name, run.github_run_id) if repo_full_name else "",
    )


def _cache_state(
    run: AsyncWorkflowRun,
    *,
    attempts_total: int,
    attempts_cached: int,
    newest_attempt_has_jobs: bool,
) -> str:
    """Describe what the local copy of a run can be trusted to answer.

    The three states are exclusive and ordered by how much the caller has to do
    about them: something is missing (``partial``), nothing is missing but the
    run is still moving (``stale``), or the local row is the final answer
    (``cached``). A live run is ``stale`` rather than ``cached`` because its
    stored status and timestamps are only as new as the last webhook.
    """
    if attempts_cached < attempts_total or not newest_attempt_has_jobs:
        return CACHE_STATE_PARTIAL
    if run.latest_status != "completed":
        return CACHE_STATE_STALE
    return CACHE_STATE_CACHED


def _to_job_record(
    job: AsyncJob,
    run: AsyncWorkflowRun | None = None,
    steps: list[AsyncStep] | None = None,
    log_sizes: dict[int, int] | None = None,
) -> JobRecord:
    """Convert a stored job into its public view.

    Args:
        job: The stored job.
        run: The job's run, used for the GitHub-facing ``run_id``.
        steps: The job's stored steps; omit only when the caller deliberately
            wants a job summary without its steps.
    """
    record = JobRecord(
        job_id=job.github_job_id,
        run_id=run.github_run_id if run is not None else 0,
        name=job.name,
        status=job.status,
        conclusion=job.conclusion,
        runner_name=job.runner_name,
        started_at=as_utc(job.started_at),
        completed_at=as_utc(job.completed_at),
        failed=(job.conclusion or "").lower() in FAILED_CONCLUSIONS,
    )
    if steps:
        sizes = log_sizes or {}
        record.steps = [_to_step_record(step, log_lines=sizes.get(step.id or 0)) for step in steps]
    return record


def _to_step_record(step: AsyncStep, *, log_lines: int | None = None) -> StepRecord:
    """Convert a stored step into its public view.

    ``log_lines`` is looked up by the caller in one batch for the whole job (or
    run): a summary that asked the database once per step would cost ten
    queries to say "these ten have logs".
    """
    return StepRecord(
        step_id=step.github_step_id,
        name=step.name,
        status=step.status,
        conclusion=step.conclusion,
        number=step.number,
        started_at=as_utc(step.started_at),
        completed_at=as_utc(step.completed_at),
        has_log=log_lines is not None,
        log_lines=log_lines,
        failed=(step.conclusion or "").lower() in FAILED_CONCLUSIONS,
    )


def _webhook_defaults() -> dict[str, str]:
    """Read the webhook URL and secret from the environment."""
    return {
        "url": _clean_str(os.getenv(ENV_WEBHOOK_URL, "")),
        "secret": _clean_str(os.getenv(ENV_WEBHOOK_SECRET, "")),
    }


def _elapsed_seconds(
    started_at: datetime | None,
    completed_at: datetime | None,
) -> float | None:
    """Elapsed seconds between two timestamps."""
    if started_at is None:
        return None
    end = completed_at or datetime.now(started_at.tzinfo)
    return (end - started_at).total_seconds()
