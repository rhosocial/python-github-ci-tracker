# tests/controllers/test_step_log_capture.py
"""Capturing step logs, reading them back, and what happens when it is too late.

The window is the whole point of this flow: GitHub carries per-step files for
about an hour after a run finishes and then serves a flat archive, so a capture
that misses the window cannot be redone. These tests pin the decisions that
follow from that:

* a completed run inside the window stores one row per step it found;
* a second capture does no network work at all;
* a run past GitHub's deadline, a disabled capture, and a flat archive all store
  say *which* of those happened;
* a GitHub failure is reported, never raised, because the caller has a job log
  to fall back on;
* asking for one step or for the failed steps never answers with another
  step's text, and the plain job-log path is untouched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import FakeGithub, make_archive, subscribe

from rhosocial.github_ci_tracker.config import LogSettings
from rhosocial.github_ci_tracker.controllers import RunData, RunLogCapture
from rhosocial.github_ci_tracker.controllers.query import QueryController
from rhosocial.github_ci_tracker.controllers.run import RunController
from rhosocial.github_ci_tracker.models import (
    AsyncJob,
    AsyncStepLog,
    AsyncWorkflowRun,
)
from rhosocial.github_ci_tracker.services.github import GithubRepositories, GithubRuns

REPO = "acme/demo"
RUN_ID = 900_500
JOB_ID = 700_500


def _fake() -> FakeGithub:
    """A public repository: an anonymous subscription only covers those."""
    fake = FakeGithub()
    fake.repo_info = {
        "id": 91,
        "owner": {"login": "acme"},
        "name": "demo",
        "full_name": REPO,
        "is_private": False,
        "default_branch": "main",
    }
    # The single-job endpoint the healing paths use answers from this list.
    fake.jobs = [{**_job_payload(), "run_id": RUN_ID}]
    return fake


def _stamp(minutes_ago: int = 0) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_payload(*, minutes_ago: int = 0, status: str = "completed") -> dict:
    return {
        "id": RUN_ID,
        "run_number": 3,
        "run_attempt": 1,
        "event": "push",
        "status": status,
        "conclusion": "failure" if status == "completed" else None,
        "head_branch": "main",
        "head_sha": "a" * 40,
        "run_started_at": _stamp(minutes_ago + 5),
        "completed_at": _stamp(minutes_ago) if status == "completed" else None,
        "workflow_id": 7,
        "path": ".github/workflows/ci.yml",
        "name": "CI",
    }


def _job_payload() -> dict:
    return {
        "id": JOB_ID,
        "name": "build",
        "status": "completed",
        "conclusion": "failure",
        "runner_name": "ubuntu-latest",
        "started_at": _stamp(5),
        "completed_at": _stamp(0),
        "steps": [
            {
                "number": 1,
                "name": "Set up job",
                "status": "completed",
                "conclusion": "success",
                "started_at": _stamp(5),
                "completed_at": _stamp(4),
            },
            {
                "number": 2,
                "name": "Run tests",
                "status": "completed",
                "conclusion": "failure",
                "started_at": _stamp(4),
                "completed_at": _stamp(0),
            },
            {
                "number": 3,
                "name": "Upload report",
                "status": "completed",
                "conclusion": "success",
                "started_at": _stamp(0),
                "completed_at": _stamp(0),
            },
        ],
    }


def _archive(*, steps: bool = True, missing: set[int] | None = None) -> bytes:
    entries = {
        "0_build.txt": "set up\none failure\nreport\n",
        "build/system.txt": "Requested labels: ubuntu-latest\n",
    }
    if steps:
        files = {
            1: ("build/1_Set up job.txt", "set up\n"),
            2: ("build/2_Run tests.txt", "one failure\n"),
            3: ("build/3_Upload report.txt", "report\n"),
        }
        for number, (name, text) in files.items():
            if missing and number in missing:
                continue
            entries[name] = text
    return make_archive(entries)


async def _ready(fake: FakeGithub) -> tuple[object, AsyncWorkflowRun, AsyncJob]:
    """A subscribed repository, a stored run and job, and the fake's archive."""
    repo = await subscribe(fake, full_name=REPO)
    run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload())
    job = await AsyncJob.upsert_from_remote(run, _job_payload())
    fake.run_log_archive = _archive()
    snapshot = await AsyncWorkflowRun.find_one(AsyncWorkflowRun.c.github_run_id == RUN_ID)
    assert snapshot is not None
    return repo, snapshot, job


def _capture(fake: FakeGithub, settings: LogSettings | None = None) -> RunLogCapture:
    # No sleeping in tests: the retry count and the delay are the same knobs the
    # deployment uses, so the loop is exercised without six minutes of waiting.
    return RunLogCapture(
        readers=GithubRuns(fake),
        runs_data=RunData(),
        settings=settings or LogSettings(capture_retry_attempts=2, capture_retry_delay_seconds=0),
    )


def _query(fake: FakeGithub, settings: LogSettings | None = None) -> QueryController:
    store = RunData()
    runs = GithubRuns(fake)
    return QueryController(
        readers=runs,
        repositories=GithubRepositories(fake),
        runs=RunController(runs=runs, runs_data=store),
        runs_data=store,
        logs=_capture(fake, settings),
    )


class TestCapture:
    async def test_a_completed_run_inside_the_window_stores_every_step(self, bound) -> None:
        fake = _fake()
        repo, run, job = await _ready(fake)

        report = await _capture(fake).ensure(repo, run)

        assert report.form == "step"
        assert (report.steps_stored, report.steps_skipped) == (3, 0)
        assert (report.jobs_matched, report.jobs_missing) == (1, 0)
        assert report.skipped_reason is None
        assert (
            await AsyncStepLog.stored_count_for_steps([step.id or 0 for step in await job.steps()])
            == 3
        )

    async def test_a_second_capture_does_no_network_work(self, bound) -> None:
        fake = _fake()
        repo, run, _ = await _ready(fake)
        capture = _capture(fake)
        await capture.ensure(repo, run)
        calls = len(fake.run_log_calls)

        report = await capture.ensure(repo, run)

        assert report.skipped_reason == "already_captured"
        assert len(fake.run_log_calls) == calls

    async def test_an_expired_run_is_not_even_asked_for(self, bound) -> None:
        """Two hours past completion the archive is flat; fetching it is waste."""
        fake = _fake()
        repo = await subscribe(fake, full_name=REPO)
        await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(minutes_ago=120))
        snapshot = await AsyncWorkflowRun.find_one(AsyncWorkflowRun.c.github_run_id == RUN_ID)
        assert snapshot is not None

        report = await _capture(fake).ensure(repo, snapshot)

        assert report.skipped_reason == "capture_closed"
        assert fake.run_log_calls == []

    async def test_a_flat_archive_stores_nothing_and_says_why(self, bound) -> None:
        fake = _fake()
        repo, run, _ = await _ready(fake)
        fake.run_log_archive = _archive(steps=False)

        report = await _capture(fake).ensure(repo, run)

        assert report.form == "job"
        assert report.steps_stored == 0
        assert report.skipped_reason == "not_a_step_archive"

    async def test_capture_can_be_switched_off(self, bound) -> None:
        fake = _fake()
        repo, run, _ = await _ready(fake)

        report = await _capture(fake, LogSettings(capture_enabled=False)).ensure(repo, run)

        assert report.skipped_reason == "capture_disabled"
        assert fake.run_log_calls == []

    async def test_a_github_failure_is_reported_not_raised(self, bound) -> None:
        fake = _fake()
        repo, run, _ = await _ready(fake)
        fake.run_log_archive = None  # the fake raises 404

        report = await _capture(fake).ensure(repo, run)

        assert report.steps_stored == 0
        assert report.skipped_reason == "archive_unavailable"
        assert report.error and "GithubAPIError" in report.error

    async def test_a_running_run_is_left_for_its_completion(self, bound) -> None:
        fake = _fake()
        repo = await subscribe(fake, full_name=REPO)
        await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(status="in_progress"))
        snapshot = await AsyncWorkflowRun.find_one(AsyncWorkflowRun.c.github_run_id == RUN_ID)
        assert snapshot is not None

        report = await _capture(fake).ensure(repo, snapshot)

        assert report.skipped_reason == "run_not_finished"


class TestOneJobAtATime:
    """A finished job is capturable while the rest of the run is still going."""

    async def test_a_finished_job_of_a_running_run_is_captured(self, bound) -> None:
        fake = _fake()
        repo = await subscribe(fake, full_name=REPO)
        await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(status="in_progress"))
        run = await AsyncWorkflowRun.find_one(AsyncWorkflowRun.c.github_run_id == RUN_ID)
        assert run is not None
        job = await AsyncJob.upsert_from_remote(run, _job_payload())
        fake.run_log_archive = _archive()

        report = await _capture(fake).ensure(repo, run, job=job)

        assert report.steps_stored == 3
        assert report.skipped_reason is None

    async def test_a_running_job_is_not_captured_yet(self, bound) -> None:
        fake = _fake()
        repo = await subscribe(fake, full_name=REPO)
        await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(status="in_progress"))
        run = await AsyncWorkflowRun.find_one(AsyncWorkflowRun.c.github_run_id == RUN_ID)
        assert run is not None
        payload = _job_payload()
        payload.update({"status": "in_progress", "conclusion": None, "completed_at": None})
        job = await AsyncJob.upsert_from_remote(run, payload)

        report = await _capture(fake).ensure(repo, run, job=job)

        assert report.skipped_reason == "job_not_finished"
        assert fake.run_log_calls == []

    async def test_the_window_is_the_jobs_own(self, bound) -> None:
        """A long run can finish hours after an early job; that job is gone."""
        fake = _fake()
        repo = await subscribe(fake, full_name=REPO)
        await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(status="in_progress"))
        run = await AsyncWorkflowRun.find_one(AsyncWorkflowRun.c.github_run_id == RUN_ID)
        assert run is not None
        payload = _job_payload()
        payload.update(
            {
                "started_at": _stamp(180),
                "completed_at": _stamp(150),
            }
        )
        job = await AsyncJob.upsert_from_remote(run, payload)

        report = await _capture(fake).ensure(repo, run, job=job)

        assert report.skipped_reason == "capture_closed"
        assert fake.run_log_calls == []

    async def test_a_flat_archive_stores_nothing_for_this_job(self, bound) -> None:
        fake = _fake()
        repo, run, job = await _ready(fake)
        fake.run_log_archive = _archive(steps=False)

        report = await _capture(fake).ensure(repo, run, job=job)

        assert report.steps_stored == 0
        assert report.skipped_reason == "not_a_step_archive"


class TestTheJobSummary:
    """What a caller reads first: the job, its steps, and which logs exist."""

    async def test_a_summary_says_which_steps_have_a_log(self, bound) -> None:
        fake = _fake()
        await _ready(fake)
        service = _query(fake)

        before = await service.get_job_detail(REPO, JOB_ID)
        assert [step.has_log for step in before.steps] == [False, False, False]

        await service.get_job_logs(REPO, JOB_ID, step=2)  # captures the job

        after = await service.get_job_detail(REPO, JOB_ID)
        assert [step.has_log for step in after.steps] == [True, True, True]
        assert all(step.log_lines for step in after.steps)
        assert after.failed is True, "the job's conclusion says failure"
        failing = next(step for step in after.steps if step.number == 2)
        assert failing.failed is True
        assert failing.duration_seconds is not None

    async def test_a_null_conclusion_is_not_a_failure(self, bound) -> None:
        """GitHub leaves the conclusion unset while metadata materializes.

        Treating that as a failure made ``failed_steps_only`` report green steps
        for a run whose step rows had not been filled in yet.
        """
        from rhosocial.github_ci_tracker.models import AsyncStep

        fake = _fake()
        await _ready(fake)
        service = _query(fake)
        await service.get_job_logs(REPO, JOB_ID, step=2)

        step = await AsyncStep.find_one(AsyncStep.c.github_step_id == 2)
        assert step is not None
        step.conclusion = None
        await step.save()

        view = await service.get_job_logs(REPO, JOB_ID, failed_steps_only=True)

        assert view.sections == []
        assert view.reason == "no_failed_steps"
        detail = await service.get_job_detail(REPO, JOB_ID)
        assert next(item for item in detail.steps if item.number == 2).failed is False


class TestTheJobDelivery:
    async def test_a_completed_job_delivery_does_not_capture(self, bound) -> None:
        """The archive is per run and 404s until the run finishes.

        Measured on a 12-job matrix: every job completion answered ``404 Not
        Found``, so capturing there stored nothing and logged a warning per job.
        Capture belongs to the run's completion and to reads.
        """
        from conftest import make_webhook_service

        fake = _fake()
        repo = await subscribe(fake, full_name=REPO)
        await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(status="in_progress"))
        fake.run_log_archive = _archive()
        service = make_webhook_service(client=fake, logs=_capture(fake))

        payload = {
            "action": "completed",
            "repository": {"id": 91, "full_name": REPO},
            "workflow_job": {**_job_payload(), "run_id": RUN_ID},
        }
        assert await service.handle_workflow_job(payload) is True

        assert fake.run_log_calls == [], "a job completion is not a capture trigger"


class TestArchiveNotReady:
    async def test_a_404_is_retried_by_a_read_until_the_archive_appears(self, bound) -> None:
        from rhosocial.github_ci_tracker.core.exceptions import GithubAPIError

        fake = _fake()
        repo, run, job = await _ready(fake)
        calls: list[int] = []

        async def not_yet(token=None, repo_full_name="", run_id=0):  # noqa: ANN001, ANN202
            calls.append(run_id)
            if len(calls) == 1:
                raise GithubAPIError("Not Found", status_code=404)
            return _archive()

        fake.get_run_logs = not_yet  # type: ignore[method-assign]
        settings = LogSettings(capture_retry_attempts=3, capture_retry_delay_seconds=0)

        report = await _capture(fake, settings).ensure(repo, run, job=job, want={2}, retry=True)

        assert len(calls) == 2, "404 is 'not ready yet' while the read may wait"
        assert report.attempts == 2
        assert report.steps_stored == 3
        assert report.error is None

    async def test_a_404_without_retry_still_reports_unavailable(self, bound) -> None:
        from rhosocial.github_ci_tracker.core.exceptions import GithubAPIError

        fake = _fake()
        repo, run, job = await _ready(fake)

        async def never(token=None, repo_full_name="", run_id=0):  # noqa: ANN001, ANN202
            raise GithubAPIError("Not Found", status_code=404)

        fake.get_run_logs = never  # type: ignore[method-assign]

        report = await _capture(fake).ensure(repo, run, job=job, want={2})

        assert report.attempts == 1
        assert report.skipped_reason == "archive_unavailable"
        assert report.error and "GithubAPIError" in report.error


class TestStepReads:
    async def test_one_step_returns_only_that_step(self, bound) -> None:
        fake = _fake()
        await _ready(fake)
        service = _query(fake)

        view = await service.get_job_logs(REPO, JOB_ID, step=2)

        assert view.source == "step"
        assert [section.number for section in view.sections] == [2]
        assert view.sections[0].content == "one failure\n"
        assert view.sections[0].name == "Run tests", "the step row's name, not the file's"
        assert view.steps_without_logs == []

    async def test_an_uncaptured_step_is_reported_as_missing(self, bound) -> None:
        fake = _fake()
        await _ready(fake)
        fake.run_log_archive = _archive(steps=False)
        service = _query(fake)

        view = await service.get_job_logs(REPO, JOB_ID, step=2)

        assert view.source == "step"
        assert view.sections == []
        assert view.steps_without_logs == [2]
        assert view.reason == "not_a_step_archive", "the capture's verdict, not a guess"

    async def test_failed_steps_only_returns_the_failing_ones(self, bound) -> None:
        fake = _fake()
        await _ready(fake)
        service = _query(fake)

        view = await service.get_job_logs(REPO, JOB_ID, failed_steps_only=True)

        assert view.source == "step"
        assert [section.number for section in view.sections] == [2]
        assert view.sections[0].conclusion == "failure"

    async def test_the_plain_read_still_uses_the_job_endpoint(self, bound) -> None:
        """Step logs must not change what a caller who asks for a job gets."""
        fake = _fake()
        await _ready(fake)
        fake.logs = {str(JOB_ID): "the merged text\n"}
        service = _query(fake)

        view = await service.get_job_logs(REPO, JOB_ID)

        assert view.source == "job"
        assert view.log_content == "the merged text\n"
        assert fake.log_calls == [JOB_ID]


class TestCaptureRetries:
    """GitHub writes the step files after the job completes; a miss is "not yet"."""

    @staticmethod
    def _staged(fake: FakeGithub, *archives: bytes) -> list[int]:
        calls: list[int] = []

        async def staged(token=None, repo_full_name="", run_id=0):  # noqa: ANN001, ANN202
            calls.append(run_id)
            index = min(len(calls), len(archives)) - 1
            return archives[index]

        fake.get_run_logs = staged  # type: ignore[method-assign]
        return calls

    async def test_a_late_step_file_is_picked_up_by_the_retry(self, bound) -> None:
        fake = _fake()
        repo, run, job = await _ready(fake)
        calls = self._staged(fake, _archive(missing={2}), _archive())
        settings = LogSettings(capture_retry_attempts=3, capture_retry_delay_seconds=0)

        report = await _capture(fake, settings).ensure(repo, run, job=job, want={2}, retry=True)

        assert len(calls) == 2, "one more fetch after the file appeared"
        assert report.attempts == 2
        assert report.steps_stored == 3, "steps 1 and 3 in the first pass, 2 in the second"
        assert report.missing_steps == []

    async def test_without_retry_the_gap_is_reported_not_hidden(self, bound) -> None:
        fake = _fake()
        repo, run, job = await _ready(fake)
        self._staged(fake, _archive(missing={2}), _archive())

        report = await _capture(fake).ensure(repo, run, job=job, want={2})

        assert report.attempts == 1
        assert report.missing_steps == [2]
        assert report.skipped_reason == "partial_capture"

    async def test_a_read_waits_only_for_the_step_it_asked_for(self, bound) -> None:
        fake = _fake()
        repo, run, job = await _ready(fake)
        calls = self._staged(fake, _archive(missing={3}), _archive())
        settings = LogSettings(capture_retry_attempts=3, capture_retry_delay_seconds=0)

        report = await _capture(fake, settings).ensure(repo, run, job=job, want={1}, retry=True)

        assert len(calls) == 1, "step 3 is not this read's problem"
        assert report.missing_steps == []


class TestHealingAStaleJob:
    """A completed job whose steps are still 'in_progress' is a missed write.

    Observed on a real run: the workflow_job completion arrived (the ledger says
    processed) but the payload carried no steps, so the job moved on and its
    steps stayed at whatever the running payload said.
    """

    async def test_a_completion_without_steps_reads_the_job_back(self, bound) -> None:
        from conftest import make_webhook_service

        fake = _fake()
        repo = await subscribe(fake, full_name=REPO)
        await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(status="in_progress"))
        service = make_webhook_service(client=fake, logs=_capture(fake))
        payload = {
            "action": "completed",
            "repository": {"id": 91, "full_name": REPO},
            "workflow_job": {**_job_payload(), "run_id": RUN_ID, "steps": []},
        }

        assert await service.handle_workflow_job(payload) is True

        assert fake.get_job_calls == [JOB_ID], "one job read, not the whole matrix"
        stored = await AsyncJob.find_one(AsyncJob.c.github_job_id == JOB_ID)
        assert stored is not None
        assert [step.status for step in await stored.steps()] == ["completed"] * 3

    async def test_a_read_repairs_a_finished_job_with_running_steps(self, bound) -> None:
        from rhosocial.github_ci_tracker.models import AsyncStep

        fake = _fake()
        await _ready(fake)
        service = _query(fake)
        step = await AsyncStep.find_one((AsyncStep.c.github_step_id == 2), derived=False)
        assert step is not None
        step.status = "in_progress"
        step.conclusion = None
        step.completed_at = None
        await step.save()

        record = await service.get_job_detail(REPO, JOB_ID)

        assert fake.get_job_calls == [JOB_ID]
        repaired = next(item for item in record.steps if item.number == 2)
        assert repaired.status == "completed"
        assert repaired.conclusion == "failure"


class TestCaptureStatus:
    async def test_the_job_says_how_much_was_captured(self, bound) -> None:
        fake = _fake()
        await _ready(fake)
        service = _query(fake)

        before = await service.get_job_detail(REPO, JOB_ID)
        assert before.capture is not None
        assert (before.capture.expected_logs, before.capture.captured_logs) == (3, 0)
        assert before.capture.state == "none"
        assert before.capture.window_closes_at is not None

        await service.get_job_logs(REPO, JOB_ID, step=2)

        after = await service.get_job_detail(REPO, JOB_ID)
        assert after.capture is not None
        assert (after.capture.expected_logs, after.capture.captured_logs) == (3, 3)
        assert after.capture.state == "complete"


class TestRefreshLogs:
    async def test_a_refresh_captures_what_the_first_pass_missed(self, bound) -> None:
        fake = _fake()
        repo, run, job = await _ready(fake)
        fake.run_log_archive = _archive(steps=False)
        service = _query(fake)
        first = await service.get_job_logs(REPO, JOB_ID, step=2)
        assert first.sections == [], "the flat archive stored nothing"

        fake.run_log_archive = _archive()
        report = await service.refresh_step_logs(REPO, JOB_ID)

        assert report.steps_stored == 3
        assert report.missing_steps == []
        detail = await service.get_job_detail(REPO, JOB_ID)
        assert detail.capture is not None and detail.capture.state == "complete"

    async def test_the_status_says_expired_once_the_window_closed(self, bound) -> None:
        fake = _fake()
        repo = await subscribe(fake, full_name=REPO)
        await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(minutes_ago=120))
        run = await AsyncWorkflowRun.find_one(AsyncWorkflowRun.c.github_run_id == RUN_ID)
        assert run is not None
        old_job = _job_payload()
        old_job.update({"started_at": _stamp(125), "completed_at": _stamp(120)})
        await AsyncJob.upsert_from_remote(run, old_job)
        service = _query(fake)

        detail = await service.get_job_detail(REPO, JOB_ID)

        assert detail.capture is not None
        assert detail.capture.state == "closed", "retrying now would be pointless"
        assert detail.capture.captured_logs == 0
