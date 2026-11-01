# tests/core/test_job_before_run.py
"""A job event that arrives before its run is adopted, not dropped.

GitHub emits one ``workflow_job`` event per job, and on a wide matrix the first
of them can reach us before the ``workflow_run`` event that describes the run.
The handler therefore fetches the parent run once instead of discarding the job.

The two refusals matter as much as the adoption:

* a job of an unsubscribed repository creates nothing (the delivery is still
  recorded in the ledger as unhandled);
* a failed fetch leaves no row and does not raise -- the next job event of the
  same run, or the run event itself, will fill the gap.
"""

from __future__ import annotations

from conftest import FakeGithub, make_webhook_service

from rhosocial.github_ci_tracker.models import (
    AsyncJob,
    AsyncRepository,
    AsyncRunAttempt,
    AsyncWorkflowRun,
)

REPO = "acme/demo"
RUN_ID = 5150
JOB_ID = 9001


def _remote_run() -> dict:
    return {
        "id": RUN_ID,
        "run_number": 3,
        "run_attempt": 1,
        "status": "in_progress",
        "conclusion": None,
        "head_branch": "main",
        "head_sha": "b" * 40,
        "event": "push",
        "workflow_id": 7,
        "path": ".github/workflows/test.yml",
        "name": "CI",
    }


def _job_payload(*, repo: str = REPO, run_id: int = RUN_ID) -> dict:
    return {
        "action": "in_progress",
        "repository": {"id": 2, "full_name": repo},
        "workflow_job": {
            "id": JOB_ID,
            "run_id": run_id,
            "name": "tests",
            "status": "in_progress",
            "conclusion": None,
            "steps": [{"number": 1, "name": "run", "status": "in_progress"}],
        },
    }


async def _subscribe(full_name: str = REPO) -> AsyncRepository:
    owner, _, name = full_name.partition("/")
    repo = AsyncRepository(github_repo_id=31, full_name=full_name, owner=owner, name=name)
    await repo.save()
    return repo


class TestJobBeforeRun:
    async def test_the_job_is_kept_when_the_run_is_unknown(self, bound) -> None:
        await _subscribe()
        client = FakeGithub(runs=[_remote_run()])
        handler = make_webhook_service(client=client)

        handled = await handler.handle_event("workflow_job", _job_payload())

        assert handled is True, "the job must not be dropped just because it outran the run"
        run = await AsyncWorkflowRun.find_by_github_id(RUN_ID)
        assert run is not None, "the parent run is fetched from GitHub and stored"
        assert run.branch == "main"
        job = await AsyncJob.find_by_github_id(JOB_ID)
        assert job is not None and job.name == "tests"
        # A job hangs off an *attempt*, and the attempt hangs off the run: the
        # job is tied to exactly the run that was adopted for it.
        attempt = await AsyncRunAttempt.find_one(AsyncRunAttempt.c.id == job.attempt_id)
        assert attempt is not None
        assert attempt.run_id == run.id

    async def test_an_unsubscribed_repository_creates_nothing(self, bound) -> None:
        client = FakeGithub(runs=[_remote_run()])
        handler = make_webhook_service(client=client)

        handled = await handler.handle_event("workflow_job", _job_payload(repo="acme/not-tracked"))

        assert handled is False
        assert await AsyncWorkflowRun.find_by_github_id(RUN_ID) is None
        assert await AsyncJob.find_by_github_id(JOB_ID) is None

    async def test_a_failed_fetch_is_not_an_error(self, bound) -> None:
        """The fake 404s an unknown run; the handler must swallow that."""
        await _subscribe()
        handler = make_webhook_service(client=FakeGithub(runs=[]))

        handled = await handler.handle_event("workflow_job", _job_payload())

        assert handled is False
        assert await AsyncWorkflowRun.find_by_github_id(RUN_ID) is None
        assert await AsyncJob.find_by_github_id(JOB_ID) is None

    async def test_the_second_job_of_a_run_does_not_fetch_it_again(self, bound) -> None:
        """Siblings share the adoption; each extra read is a wasted API call."""
        await _subscribe()
        client = FakeGithub(runs=[_remote_run()])
        handler = make_webhook_service(client=client)

        await handler.handle_event("workflow_job", _job_payload())
        second = _job_payload()
        second["workflow_job"]["id"] = JOB_ID + 1
        handled = await handler.handle_event("workflow_job", second)

        assert handled is True
        assert client.run_calls == [RUN_ID]

    async def test_adopting_the_same_run_twice_returns_the_stored_row(self, bound) -> None:
        """Two deliveries can both find the run missing before either inserts it.

        The loser of that race must re-read the winner's row: failing the whole
        delivery loses the job it carried, and shows up as an ``IntegrityError``
        in the ledger with nothing to repair it.
        """
        repo = await _subscribe()

        first = await AsyncWorkflowRun.adopt_from_remote(repo, _remote_run())
        second = await AsyncWorkflowRun.adopt_from_remote(repo, _remote_run())

        assert second.id == first.id
        assert await AsyncWorkflowRun.query().count() == 1
