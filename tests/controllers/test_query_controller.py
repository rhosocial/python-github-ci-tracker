# tests/controllers/test_query_controller.py
"""End to end for ``QueryController``: fake transport, real models, real database.

The read path has two sources and the tests pin which one answers:

* the rows, when they can answer -- a finished run is not re-read;
* GitHub, when they cannot -- an unknown run, a live one, a finished run whose
  jobs were never stored, a log nobody fetched.

Only the transport is faked. The adopt path, the state machine, the attempt
rows, the job/step cascade and the log row are real.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from conftest import FakeGithub

from rhosocial.github_ci_tracker.controllers import RunData
from rhosocial.github_ci_tracker.controllers.query import QueryController
from rhosocial.github_ci_tracker.controllers.run import RunController
from rhosocial.github_ci_tracker.core.exceptions import (
    AttemptNotFoundError,
    GithubAPIError,
    JobNotFoundError,
    RepositoryNotFoundError,
    RunNotFoundError,
    WorkflowNotFoundError,
)
from rhosocial.github_ci_tracker.models import (
    AsyncJob,
    AsyncRepository,
    AsyncWorkflow,
    AsyncWorkflowRun,
)
from rhosocial.github_ci_tracker.models.attempt import AsyncRunAttempt
from rhosocial.github_ci_tracker.models.log import AsyncJobLog
from rhosocial.github_ci_tracker.services.github import GithubRepositories, GithubRuns

REPO = "acme/demo"
RUN_ID = 500
JOB_ID = 900
WORKFLOW_PATH = ".github/workflows/ci.yml"


async def _tracked(full_name: str = REPO) -> AsyncRepository:
    """Store a repository and the one workflow the tests query."""
    owner, name = full_name.split("/", 1)
    repo = AsyncRepository(
        github_repo_id=hash(full_name) % 100_000,
        full_name=full_name,
        owner=owner,
        name=name,
    )
    await repo.save()
    await AsyncWorkflow(
        repository_id=repo.id or 0,
        workflow_id=7,
        name="CI",
        path=WORKFLOW_PATH,
    ).save()
    return repo


def _service(fake: FakeGithub) -> QueryController:
    runs = GithubRuns(fake)
    store = RunData()
    return QueryController(
        readers=runs,
        repositories=GithubRepositories(fake),
        runs=RunController(runs=runs, runs_data=store),
        runs_data=store,
    )


class TestGetRunStatus:
    async def test_an_unknown_run_is_adopted(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()

        record = await _service(fake).get_run_status(REPO, RUN_ID)

        assert record.run_id == RUN_ID
        assert record.status == "completed"
        assert fake.run_calls == [RUN_ID]

    async def test_a_finished_run_is_still_read_from_github(self, bound) -> None:
        """A terminal row is not "settled enough to skip": it can be re-run, and
        a webhook may never have arrived."""
        await _tracked()
        fake = FakeGithub()
        service = _service(fake)

        await service.get_run_status(REPO, RUN_ID)
        await service.get_run_status(REPO, RUN_ID)

        assert fake.run_calls == [RUN_ID, RUN_ID]

    async def test_a_failed_read_raises_instead_of_answering_from_the_cache(self, bound) -> None:
        """The cache is never a fallback: an unreachable GitHub is an error."""
        await _tracked()
        fake = FakeGithub()
        service = _service(fake)
        await service.get_run_status(REPO, RUN_ID)  # this populates the cache

        async def missing(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise GithubAPIError("missing", status_code=500)

        fake.get_run = missing  # type: ignore[method-assign]

        with pytest.raises(GithubAPIError):
            await service.get_run_status(REPO, RUN_ID)

    async def test_an_untracked_repository_is_refused(self, bound) -> None:
        with pytest.raises(RepositoryNotFoundError):
            await _service(FakeGithub()).get_run_status("nobody/nothing", RUN_ID)


class TestListRuns:
    async def test_a_conclusion_is_matched_against_the_conclusion_column(self, bound) -> None:
        """``success`` is a conclusion, not a status: filtering the wrong column
        silently returns nothing."""
        await _tracked()
        service = _service(FakeGithub())
        await service.refresh_run(REPO, RUN_ID)

        assert [record.run_id for record in await service.list_runs(REPO, status="success")] == [
            RUN_ID
        ]
        assert await service.list_runs(REPO, status="failure") == []

    async def test_an_unknown_workflow_lists_paths_because_names_move(self, bound) -> None:
        """The fallback message must teach a reference that survives a rename.

        A workflow is one id per file path, while its display name lives in that
        file and therefore differs per branch; listing names first hands the
        caller a reference that may not resolve tomorrow.
        """
        await _tracked()
        service = _service(FakeGithub())

        with pytest.raises(WorkflowNotFoundError) as caught:
            await service.list_runs(REPO, workflow="no-such-workflow")

        message = str(caught.value)
        assert ".github/workflows/ci.yml (id 7)" in message
        assert "Use a path, a file name or the numeric workflow id" in message

    async def test_a_workflow_reference_scopes_the_list(self, bound) -> None:
        await _tracked()
        service = _service(FakeGithub())
        await service.refresh_run(REPO, RUN_ID)

        assert await service.list_runs(REPO, workflow="ci.yml") != []


class TestGetLatestRun:
    """``latest`` means newest of the repository, not newest of ``main``."""

    @staticmethod
    def _two_branches() -> FakeGithub:
        return FakeGithub(
            runs=[
                {
                    "id": RUN_ID + 2,
                    "run_number": 3,
                    "status": "in_progress",
                    "head_branch": "feature/x",
                    "head_sha": "b" * 40,
                    "run_started_at": "2026-10-30T09:00:00Z",
                    "workflow_id": 7,
                    "path": WORKFLOW_PATH,
                    "name": "CI",
                },
                {
                    "id": RUN_ID + 1,
                    "run_number": 2,
                    "status": "completed",
                    "conclusion": "success",
                    "head_branch": "main",
                    "head_sha": "a" * 40,
                    "created_at": "2026-09-27T12:00:00Z",
                    "run_started_at": "2026-09-27T12:00:05Z",
                    "workflow_id": 7,
                    "path": WORKFLOW_PATH,
                    "name": "CI",
                },
            ]
        )

    async def test_without_a_branch_the_newest_run_of_any_branch_is_answered(self, bound) -> None:
        """The default branch must not be implied: it answered a month-old run."""
        await _tracked()

        record = await _service(self._two_branches()).get_latest_run(REPO)

        assert record is not None
        assert record.branch == "feature/x"

    async def test_a_branch_narrows_the_answer(self, bound) -> None:
        await _tracked()

        record = await _service(self._two_branches()).get_latest_run(REPO, branch="main")

        assert record is not None
        assert record.branch == "main"

    async def test_created_at_is_githubs_time_and_stored_at_is_ours(self, bound) -> None:
        await _tracked()

        record = await _service(self._two_branches()).get_latest_run(REPO, branch="main")

        assert record is not None
        assert record.created_at is not None
        assert record.stored_at is not None
        assert record.created_at == datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc), (
            f"github created it, not us: got {record.created_at!r} "
            f"(started {record.started_at!r}, stored {record.stored_at!r})"
        )
        assert (
            record.stored_at > record.created_at
        ), "a row stored today cannot have been created by GitHub in September"


class TestRefreshRun:
    async def test_a_refresh_adopts_the_run_and_its_jobs(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()

        result = await _service(fake).refresh_run(REPO, RUN_ID)

        assert result.created is True
        assert result.jobs_synced == 1
        assert result.steps_synced == 1
        assert await AsyncJob.query().count() == 1

    async def test_a_second_refresh_updates_instead_of_creating(self, bound) -> None:
        await _tracked()
        service = _service(FakeGithub())
        await service.refresh_run(REPO, RUN_ID)

        result = await service.refresh_run(REPO, RUN_ID)

        assert result.created is False
        assert result.jobs_synced == 1


class TestGetRunAttempt:
    """One attempt: always verified against GitHub, written back when it differs."""

    @staticmethod
    def _attempt_payload(fake: FakeGithub, **overrides: object) -> dict[str, object]:
        """A run-shaped payload for a past attempt."""
        return {
            **fake.runs[0],
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "failure",
            "run_started_at": "2026-10-01T00:00:00Z",
            "completed_at": "2026-10-01T00:05:00Z",
            **overrides,
        }

    async def _run_at_attempt_two(self, fake: FakeGithub):
        """Store a run whose newest attempt is 2, with attempt 1 available upstream."""
        fake.runs[0] = {
            **fake.runs[0],
            "run_attempt": 2,
            "status": "completed",
            "conclusion": "success",
        }
        service = _service(fake)
        await service.refresh_run(REPO, RUN_ID)
        fake.attempt_payloads[(RUN_ID, 1)] = self._attempt_payload(fake)
        fake.attempt_payloads[(RUN_ID, 2)] = {
            **fake.runs[0],
            "run_attempt": 2,
            "status": "completed",
            "conclusion": "success",
        }
        return service

    async def test_a_past_attempt_is_read_from_github_and_stored(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()
        service = await self._run_at_attempt_two(fake)

        record = await service.get_run_status(REPO, RUN_ID, attempt=1)

        assert fake.attempt_calls == [(RUN_ID, 1)], "a past attempt is never served from cache"
        assert record.attempt == 1
        assert record.conclusion == "failure"
        assert record.is_latest is False
        assert record.attempts_total == 2, "the run's newest attempt is still 2"
        assert record.cache_state != "stale", "a finished run is not stale"
        assert record.started_at is not None and record.started_at.day == 1
        assert record.started_at.utcoffset().total_seconds() == 0, "timestamps are UTC"

        run = await AsyncWorkflowRun.find_by_github_id(RUN_ID, derived=True)
        assert run is not None
        assert await AsyncRunAttempt.find_attempt(run.id or 0, 1) is not None

    async def test_a_past_attempt_carries_the_workflow_like_the_run_does(self, bound) -> None:
        """One run, two answers, one shape.

        The workflow lives in its own table -- the run row only carries
        ``workflow_id`` -- and ``records`` resolves it in a batch. The
        single-attempt path builds its view directly, so it has to resolve the
        same fact itself; skipping that made ``workflow`` null for attempt 1 and
        filled in for the current attempt, which reads as a data problem rather
        than as a call that took a different route.
        """
        await _tracked()
        fake = FakeGithub()
        service = await self._run_at_attempt_two(fake)

        current = await service.get_run_status(REPO, RUN_ID)
        past = await service.get_run_status(REPO, RUN_ID, attempt=1)

        assert current.workflow_name == "CI"
        assert past.workflow_name == current.workflow_name
        assert past.workflow_path == current.workflow_path == ".github/workflows/ci.yml"

    async def test_a_pr_run_learns_its_base_branch_from_the_pull_request(self, bound) -> None:
        """A PR number without its base branch reads as a bug, so fill it in.

        GitHub stops reporting ``pull_requests`` on a run once the pull request
        is closed, which leaves the stored run with a ``pr_number`` and no
        ``base_ref``. The base branch does not change after a merge, so one call
        to the PR settles it -- and it is written back, so the next read is free.
        """
        await _tracked()
        fake = FakeGithub()
        fake.runs[0] = {**fake.runs[0], "pull_requests": [{"number": 12}]}
        fake.pull_requests[12] = {"number": 12, "base": {"ref": "release/v1.0"}}
        service = _service(fake)

        record = await service.get_run_status(REPO, RUN_ID)

        assert record.pr_number == 12
        assert record.base_ref == "release/v1.0"

        stored = await AsyncWorkflowRun.find_by_github_id(RUN_ID, derived=True)
        assert stored is not None and stored.base_ref == "release/v1.0"

    async def test_reading_a_past_attempt_does_not_move_the_run_state(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()
        service = await self._run_at_attempt_two(fake)

        await service.get_run_status(REPO, RUN_ID, attempt=1)

        run = await AsyncWorkflowRun.find_by_github_id(RUN_ID, derived=True)
        assert run is not None
        assert (run.latest_attempt, run.latest_conclusion) == (2, "success")

    async def test_a_local_attempt_that_disagrees_is_updated(self, bound) -> None:
        """The row an older build wrote, or a lost delivery left behind, is repaired."""
        await _tracked()
        fake = FakeGithub()
        service = await self._run_at_attempt_two(fake)
        await service.get_run_status(REPO, RUN_ID, attempt=1)

        # GitHub now reports a different completion time than the stored one.
        fake.attempt_payloads[(RUN_ID, 1)] = self._attempt_payload(
            fake, completed_at="2026-10-01T00:09:00Z"
        )
        record = await service.get_run_status(REPO, RUN_ID, attempt=1)

        run = await AsyncWorkflowRun.find_by_github_id(RUN_ID)
        assert run is not None
        stored = await AsyncRunAttempt.find_attempt(run.id or 0, 1)
        assert stored is not None
        assert stored.completed_at == record.completed_at
        assert stored.completed_at.minute == 9

    async def test_an_attempt_past_the_last_one_names_the_range(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()
        service = await self._run_at_attempt_two(fake)

        with pytest.raises(AttemptNotFoundError, match="no attempt 9"):
            await service.get_run_status(REPO, RUN_ID, attempt=9)

    async def test_asking_for_the_newest_attempt_by_number_says_it_is_latest(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()
        service = await self._run_at_attempt_two(fake)

        record = await service.get_run_status(REPO, RUN_ID, attempt=2)

        assert fake.attempt_calls == [(RUN_ID, 2)]
        assert record.attempt == 2
        assert record.is_latest is True

    async def test_without_an_attempt_the_newest_answers(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()
        service = await self._run_at_attempt_two(fake)

        record = await service.get_run_status(REPO, RUN_ID)

        assert record.attempt == 2
        assert record.is_latest is True
        assert fake.attempt_calls == [], "the default read is the run endpoint"


class TestSubResourceCache:
    """Attempts, jobs, steps and logs are lazy: fetched when asked, then terminal."""

    async def test_a_finished_runs_stored_jobs_are_not_refetched(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()
        service = _service(fake)
        await service.refresh_run(REPO, RUN_ID)  # stores the run and its jobs
        fake.jobs_calls.clear()

        jobs = await service.get_run_jobs(REPO, RUN_ID)

        assert [job.job_id for job in jobs] == [JOB_ID]
        assert fake.jobs_calls == [], "a finished execution's jobs are terminal"

    async def test_a_running_runs_jobs_are_fetched(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()
        fake.runs[0] = {**fake.runs[0], "status": "in_progress", "conclusion": None}
        service = _service(fake)
        await service.refresh_run(REPO, RUN_ID)
        fake.jobs_calls.clear()

        await service.get_run_jobs(REPO, RUN_ID)

        assert fake.jobs_calls == [RUN_ID], "a running execution's job list is still growing"

    async def test_a_running_jobs_log_is_refetched_not_served_from_the_row(self, bound) -> None:
        """A running job's log is truncated by definition, so the stored copy is
        not an answer until the job finishes -- after that it is terminal."""
        await _tracked()
        fake = FakeGithub()
        fake.runs[0] = {**fake.runs[0], "status": "in_progress", "conclusion": None}
        fake.jobs[0] = {**fake.jobs[0], "status": "in_progress", "conclusion": None}
        service = _service(fake)
        await service.refresh_run(REPO, RUN_ID)

        await service.get_job_logs(REPO, JOB_ID)
        logs = await service.get_job_logs(REPO, JOB_ID)

        assert fake.log_calls == [JOB_ID, JOB_ID], "a running job's log grows"

        # The job finishes: from here the stored log is the final document.
        fake.runs[0] = {**fake.runs[0], "status": "completed", "conclusion": "success"}
        fake.jobs[0] = {**fake.jobs[0], "status": "completed", "conclusion": "success"}
        await service.refresh_run(REPO, RUN_ID)
        fake.log_calls.clear()

        await service.get_job_logs(REPO, JOB_ID)

        assert fake.log_calls == []
        assert logs.log_content == "line1\nline2\nline3"


class TestJobsAndLogs:
    async def test_jobs_carry_their_steps(self, bound) -> None:
        await _tracked()
        service = _service(FakeGithub())
        await service.refresh_run(REPO, RUN_ID)

        jobs = await service.get_run_jobs(REPO, RUN_ID)

        assert [job.job_id for job in jobs] == [JOB_ID]
        assert [step.name for step in jobs[0].steps] == ["checkout"]

    async def test_a_roster_without_steps_does_not_load_them(self, bound) -> None:
        """The run-level answer must not pay one step query per job."""
        await _tracked()
        service = _service(FakeGithub())
        await service.refresh_run(REPO, RUN_ID)

        jobs = await service.get_run_jobs(REPO, RUN_ID, with_steps=False)

        assert [job.job_id for job in jobs] == [JOB_ID]
        assert jobs[0].steps == []

    async def test_a_finished_run_without_jobs_is_fetched_once(self, bound) -> None:
        """The push channel can deliver a run before its jobs."""
        await _tracked()
        fake = FakeGithub()
        service = _service(fake)
        await service.get_run_status(REPO, RUN_ID)  # adopt the run, no jobs

        jobs = await service.get_run_jobs(REPO, RUN_ID)

        assert [job.job_id for job in jobs] == [JOB_ID]
        assert await AsyncJob.query().count() == 1

    async def test_a_log_is_fetched_once_and_then_read_from_the_row(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()
        service = _service(fake)
        await service.refresh_run(REPO, RUN_ID)

        first = await service.get_job_logs(REPO, JOB_ID)
        second = await service.get_job_logs(REPO, JOB_ID)

        assert first.log_content == "line1\nline2\nline3"
        assert second.log_content == first.log_content
        assert fake.log_calls == [JOB_ID]
        assert await AsyncJobLog.query().count() == 1

    async def test_a_tail_keeps_only_the_last_lines(self, bound) -> None:
        await _tracked()
        service = _service(FakeGithub())
        await service.refresh_run(REPO, RUN_ID)

        logs = await service.get_job_logs(REPO, JOB_ID, tail=2)

        assert logs.log_content == "line2\nline3"

    async def test_an_unknown_job_is_an_error_before_any_github_call(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()

        with pytest.raises(JobNotFoundError):
            await _service(fake).get_job_logs(REPO, 12_345)

        assert fake.log_calls == []


async def _store_execution(
    repo: AsyncRepository,
    run_number: int,
    conclusions: list[str | None],
    job_name: str = "build",
) -> None:
    """Store one run whose attempts each carry one job called ``job_name``.

    One attempt per entry in ``conclusions``: a re-run is a new attempt of the
    same run, so this is also how the "each attempt counts" case is built.
    """
    workflow = (await AsyncWorkflow.for_repository(repo.id or 0).all())[0]
    run = AsyncWorkflowRun(
        workflow_id=workflow.id or 0,
        github_run_id=10_000 + run_number,
        run_number=run_number,
        event="push",
        branch="main",
        commit_sha=f"sha-{run_number}",
    )
    await run.save()
    for number, conclusion in enumerate(conclusions, start=1):
        attempt = AsyncRunAttempt(
            run_id=run.id or 0,
            attempt_number=number,
            status="completed",
            conclusion=conclusion,
        )
        await attempt.save()
        await AsyncJob(
            github_job_id=20_000 + run_number * 10 + number,
            attempt_id=attempt.id or 0,
            name=job_name,
            status="completed",
            conclusion=conclusion,
        ).save()


class TestJobFailureHistory:
    """The "re-run or fix it?" history: local rows, and a verdict from them."""

    async def test_a_job_that_always_passed_says_never(self, bound) -> None:
        repo = await _tracked()
        await _store_execution(repo, 1, ["success"])
        await _store_execution(repo, 2, ["success"])

        history = await _service(FakeGithub()).job_failure_history(REPO, "build")

        assert history is not None
        assert (history.sampled_executions, history.failures) == (2, 0)
        assert history.verdict == "never"

    async def test_a_job_that_always_failed_says_always(self, bound) -> None:
        repo = await _tracked()
        await _store_execution(repo, 1, ["failure"])
        await _store_execution(repo, 2, ["failure"])

        history = await _service(FakeGithub()).job_failure_history(REPO, "build")

        assert history is not None
        assert history.verdict == "always"

    async def test_a_job_that_failed_once_says_intermittent(self, bound) -> None:
        repo = await _tracked()
        await _store_execution(repo, 1, ["success"])
        await _store_execution(repo, 2, ["failure"])

        history = await _service(FakeGithub()).job_failure_history(REPO, "build")

        assert history is not None
        assert (history.sampled_executions, history.failures) == (2, 1)
        assert history.verdict == "intermittent"

    async def test_each_attempt_of_one_run_is_an_execution(self, bound) -> None:
        """A re-run is a new try, so both attempts of one run are sampled."""
        repo = await _tracked()
        await _store_execution(repo, 1, ["failure", "success"])

        history = await _service(FakeGithub()).job_failure_history(REPO, "build")

        assert history is not None
        assert (history.sampled_executions, history.failures) == (2, 1)

    async def test_only_the_newest_runs_are_sampled(self, bound) -> None:
        repo = await _tracked()
        for run_number in range(1, 5):
            await _store_execution(repo, run_number, ["failure"])

        history = await _service(FakeGithub()).job_failure_history(REPO, "build", sample=2)

        assert history is not None
        assert history.sampled_executions == 2

    async def test_an_untracked_repository_or_unknown_job_has_no_history(self, bound) -> None:
        repo = await _tracked()
        await _store_execution(repo, 1, ["success"])
        service = _service(FakeGithub())

        assert await service.job_failure_history("acme/ghost", "build") is None
        assert await service.job_failure_history(REPO, "nope") is None


class TestRepositoryNameOfRun:
    """``owner/repo`` from a run id: the stored rows answer, not GitHub."""

    async def test_a_stored_run_resolves_its_repository(self, bound) -> None:
        await _tracked()
        service = _service(FakeGithub())
        await service.refresh_run(REPO, RUN_ID)

        assert await service.repository_name_of_run(RUN_ID) == REPO

    async def test_an_unknown_run_raises_instead_of_guessing(self, bound) -> None:
        await _tracked()

        with pytest.raises(RunNotFoundError):
            await _service(FakeGithub()).repository_name_of_run(999_999)


class TestRunIdentity:
    """A run must say which workflow it is -- two runs of one sha differ only there."""

    async def test_the_record_carries_the_workflow_name_and_path(self, bound) -> None:
        await _tracked()
        record = await _service(FakeGithub()).get_run_status(REPO, RUN_ID)

        assert record.workflow_name == "CI"
        assert record.workflow_path == WORKFLOW_PATH

    async def test_a_cached_workflow_without_an_id_does_not_drop_the_filter(self, bound) -> None:
        """``workflow_id = 0`` is "not resolved yet", never "all workflows".

        Returning ``None`` there sent the query without a filter, so
        ``recent_runs(workflow=...)`` answered with every workflow's runs and
        said nothing about it.
        """
        from rhosocial.github_ci_tracker.models import AsyncWorkflow

        repo = await _tracked()
        cached = await AsyncWorkflow.find_one(AsyncWorkflow.c.repository_id == (repo.id or 0))
        assert cached is not None
        cached.workflow_id = 0
        await cached.save()

        fake = FakeGithub(
            runs=[
                {**_run_payload(), "id": RUN_ID, "workflow_id": 7},
                {**_run_payload(), "id": RUN_ID + 1, "workflow_id": 8},
            ]
        )
        records = await _service(fake).list_runs(REPO, workflow="ci.yml")

        assert [record.run_id for record in records] == [RUN_ID]
        stored = await AsyncWorkflow.find_one(AsyncWorkflow.c.repository_id == (repo.id or 0))
        assert stored is not None and stored.workflow_id == 7, "the resolved id is remembered"


def _run_payload() -> dict:
    """A minimal run payload; the tests overlay the fields they are about."""
    return {
        "run_number": 1,
        "run_attempt": 1,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "head_branch": "main",
        "head_sha": "abc",
        "run_started_at": "2026-10-10T10:00:00Z",
        "completed_at": "2026-10-10T10:05:00Z",
        "workflow_id": 7,
        "path": WORKFLOW_PATH,
        "name": "CI",
    }


class TestCommitContext:
    async def test_the_record_carries_the_commit_author_and_time(self, bound) -> None:
        await _tracked()
        fake = FakeGithub(
            runs=[
                {
                    **_run_payload(),
                    "id": RUN_ID,
                    "head_commit": {
                        "message": "fix: the thing",
                        "timestamp": "2026-10-09T08:00:00Z",
                        "author": {"name": "Alice", "email": "alice@example.com"},
                    },
                    "display_title": "fix: the thing (#12)",
                    "head_repository": {"full_name": "fork/demo"},
                    "pull_requests": [{"number": 12, "base": {"ref": "main"}}],
                }
            ]
        )

        record = await _service(fake).get_run_status(REPO, RUN_ID)

        assert record.commit_message == "fix: the thing"
        assert record.commit_author == "Alice"
        assert record.commit_time is not None
        assert record.commit_time.isoformat().startswith("2026-10-09T08:00:00")
        assert record.display_title == "fix: the thing (#12)"
        assert record.head_repo == "fork/demo"
        assert record.base_ref == "main"
        assert record.pr_number == 12


class TestFailedJobsInLists:
    """One call answers "which job went red" for a batch of repositories."""

    @staticmethod
    def _fake_with_two_jobs() -> FakeGithub:
        return FakeGithub(
            jobs=[
                {
                    "id": 900,
                    "run_id": RUN_ID,
                    "name": "build",
                    "status": "completed",
                    "conclusion": "success",
                    "runner_name": "ubuntu-latest",
                    "started_at": "2026-10-10T10:00:00Z",
                    "completed_at": "2026-10-10T10:05:00Z",
                    "steps": [],
                },
                {
                    "id": 901,
                    "run_id": RUN_ID,
                    "name": "tests",
                    "status": "completed",
                    "conclusion": "failure",
                    "runner_name": "ubuntu-latest",
                    "started_at": "2026-10-10T10:00:00Z",
                    "completed_at": "2026-10-10T10:06:00Z",
                    "steps": [],
                },
            ]
        )

    async def test_list_run_summaries_reports_only_the_failing_job(self, bound) -> None:
        await _tracked()
        service = _service(self._fake_with_two_jobs())
        await service.refresh_run(REPO, RUN_ID)

        results = await service.list_run_summaries([REPO], per_page=1)

        entries = results[0].runs[0].failed_jobs
        assert entries is not None, "a list asks for it"
        assert [(job.name, job.conclusion) for job in entries] == [("tests", "failure")]

    async def test_a_job_with_no_conclusion_is_not_reported_as_failed(self, bound) -> None:
        """GitHub leaves jobs of an aborted run at ``in_progress`` forever.

        They have no verdict, so listing them reports a name with an empty
        conclusion as though it had failed.
        """
        await _tracked()
        fake = self._fake_with_two_jobs()
        fake.jobs[1]["conclusion"] = None
        fake.jobs[1]["status"] = "in_progress"
        service = _service(fake)
        await service.refresh_run(REPO, RUN_ID)

        results = await service.list_run_summaries([REPO], per_page=1)

        assert results[0].runs[0].failed_jobs == []

    async def test_a_plain_list_does_not_pay_for_them(self, bound) -> None:
        await _tracked()
        service = _service(self._fake_with_two_jobs())
        await service.refresh_run(REPO, RUN_ID)

        records = await service.list_runs(REPO)

        assert records[0].failed_jobs is None, "not asked for, so not loaded"

    async def test_a_run_with_no_failures_says_so_with_an_empty_list(self, bound) -> None:
        await _tracked()
        service = _service(FakeGithub())
        await service.refresh_run(REPO, RUN_ID)

        records = await service.list_runs(REPO)
        await service.attach_failed_jobs(records)

        assert records[0].failed_jobs == [], "asked, and everything passed"
