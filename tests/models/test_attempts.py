# tests/models/test_attempts.py
"""Run attempts: a re-run reuses the run id and increments ``run_attempt``.

GitHub's behaviour here is the reason these tests exist: ``POST /rerun`` keeps
``github_run_id`` and bumps ``run_attempt``, and ``/runs/{id}/jobs`` returns only
the latest attempt. That means a re-run's jobs are *new rows*, and the earlier
attempt's rows stay behind. Everything reporting "the jobs of this run" must
therefore select one attempt, or the same job is reported twice.

The payloads below are trimmed to the fields the models read.
"""

from __future__ import annotations

from conftest import FakeGithub, subscribe

from rhosocial.github_ci_tracker.models import (
    AsyncJob,
    AsyncRunAttempt,
    AsyncWorkflowRun,
)
from rhosocial.github_ci_tracker.models.run import _pull_request_number

RUN_ID = 777000


def _run_payload(*, attempt: int = 1, status: str = "completed", prs=None) -> dict:
    """A minimal run payload, with the attempt count under test."""
    return {
        "id": RUN_ID,
        "run_number": 42,
        "run_attempt": attempt,
        "event": "push",
        "status": status,
        "conclusion": "failure" if status == "completed" else None,
        "head_branch": "main",
        "head_sha": "a" * 40,
        "head_commit": {"message": "test"},
        "actor": {"login": "alice"},
        "run_started_at": "2026-10-20T10:00:00Z",
        "completed_at": "2026-10-20T10:10:00Z" if status == "completed" else None,
        "pull_requests": prs or [],
        "workflow_id": 7,
        "path": ".github/workflows/ci.yml",
        "name": "CI",
    }


def _job_payload(job_id: int, *, attempt: int, name: str = "build", conclusion: str = "failure"):
    """A minimal job payload carrying the attempt it belongs to."""
    return {
        "id": job_id,
        "run_id": RUN_ID,
        "run_attempt": attempt,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "runner_name": "ubuntu-latest",
        "started_at": "2026-10-20T10:00:00Z",
        "completed_at": "2026-10-20T10:05:00Z",
        "steps": [
            {
                "number": 1,
                "name": "checkout",
                "status": "completed",
                "conclusion": "success",
            }
        ],
    }


async def test_a_payload_without_an_attempt_lands_on_the_newest_one(
    fake_client: FakeGithub, bound
) -> None:
    """Older payloads omit ``run_attempt``; they describe the current execution."""
    fake_client.repo_info = {**fake_client.repo_info, "is_private": False}
    repo = await subscribe(fake_client, "acme/demo")
    run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(attempt=2))

    fragment = _run_payload(attempt=2, status="in_progress")
    del fragment["run_attempt"]
    await run.update_from_remote(fragment)

    latest = await AsyncRunAttempt.latest_for_run(run.id or 0)
    assert latest is not None and latest.attempt_number == 2
    assert await AsyncRunAttempt.query().count() == 1


def test_pull_request_helper_treats_empty_as_unknown() -> None:
    """An empty array is "unknown" (fork PRs), never "no pull request"."""
    assert _pull_request_number({}) is None
    assert _pull_request_number({"pull_requests": []}) is None
    assert _pull_request_number({"pull_requests": [{"number": 126}]}) == 126


async def test_adopt_stores_attempt_and_pr_number(fake_client: FakeGithub, bound) -> None:
    """A newly adopted run records which attempt it is and the cached PR."""
    fake_client.repo_info = {**fake_client.repo_info, "is_private": False}
    repo = await subscribe(fake_client, "acme/demo")

    run = await AsyncWorkflowRun.adopt_from_remote(
        repo, _run_payload(attempt=3, prs=[{"number": 126}])
    )
    assert run.latest_attempt == 3
    assert run.pr_number == 126

    stored = await AsyncWorkflowRun.find_by_github_id(RUN_ID, derived=True)
    assert stored is not None
    assert stored.latest_attempt == 3


async def test_update_advances_the_attempt(fake_client: FakeGithub, bound) -> None:
    """A re-run payload moves the stored attempt forward."""
    fake_client.repo_info = {**fake_client.repo_info, "is_private": False}
    repo = await subscribe(fake_client, "acme/demo")

    run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(attempt=1))
    await run.update_from_remote(_run_payload(attempt=2))
    assert run.latest_attempt == 2
    stored = await AsyncWorkflowRun.find_by_github_id(RUN_ID, derived=True)
    assert stored is not None and stored.latest_attempt == 2


async def test_jobs_of_attempt_isolates_attempts(fake_client: FakeGithub, bound) -> None:
    """Jobs from an earlier attempt must not leak into the current one.

    This is the regression that matters: after a re-run, ``/jobs`` returns new
    job ids, so the previous attempt's rows stay in the table. Reporting "the
    jobs of this run" without an attempt filter doubles the list.
    """
    fake_client.repo_info = {**fake_client.repo_info, "is_private": False}
    repo = await subscribe(fake_client, "acme/demo")

    run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(attempt=1))
    await AsyncJob.upsert_from_remote(run, _job_payload(1001, attempt=1, name="first-attempt"))

    await run.update_from_remote(_run_payload(attempt=2))
    await AsyncJob.upsert_from_remote(run, _job_payload(2001, attempt=2, name="second-attempt"))

    latest = await run.jobs_of_attempt()
    assert [job.name for job in latest] == ["second-attempt"]

    first = await run.jobs_of_attempt(1)
    assert [job.name for job in first] == ["first-attempt"]

    # The relation itself still sees both rows: history is kept on purpose.
    assert len(await run.attempts()) == 2
    all_jobs = [job for attempt in await run.attempts() for job in await attempt.jobs()]
    assert sorted(job.name for job in all_jobs) == ["first-attempt", "second-attempt"]


async def test_job_attempt_falls_back_to_the_run(fake_client: FakeGithub, bound) -> None:
    """A webhook job fragment without its own attempt inherits the run's."""
    fake_client.repo_info = {**fake_client.repo_info, "is_private": False}
    repo = await subscribe(fake_client, "acme/demo")

    run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(attempt=5))
    payload = _job_payload(3001, attempt=5)
    del payload["run_attempt"]

    job = await AsyncJob.upsert_from_remote(run, payload)
    # The fragment carried no attempt, so the run's (5) was used: the job now
    # hangs off attempt 5 rather than remembering the number itself.
    attempt = await AsyncRunAttempt.find_attempt(run.id or 0, 5)
    assert attempt is not None
    assert job.attempt_id == attempt.id


async def test_the_loser_of_a_step_insert_race_adopts_the_winner(
    fake_client: FakeGithub, bound, monkeypatch
) -> None:
    """Two writers that both miss the row must not fail the caller.

    A poll (``wait_for_run``) and a delivery can reach the same job at the same
    moment: both read "no step", both insert, and the unique key rejects the
    second. That used to abort the whole refresh with an ``IntegrityError``; the
    loser now answers with the winner's row, which is what the caller wanted
    anyway.
    """
    from rhosocial.activerecord.backend.errors import IntegrityError

    from rhosocial.github_ci_tracker.models import AsyncStep

    fake_client.repo_info = {**fake_client.repo_info, "is_private": False}
    repo = await subscribe(fake_client, "acme/demo")
    run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(attempt=1))
    # The job payload carries step 1, so the row already exists.
    job = await AsyncJob.upsert_from_remote(run, _job_payload(3001, attempt=1))

    step_payload = {"number": 1, "name": "checkout", "status": "completed", "conclusion": "success"}

    real_find_one = AsyncStep.find_one
    calls = {"lookups": 0}

    async def miss_once(cls, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["lookups"] += 1
        if calls["lookups"] == 1:
            return None  # the racing writer had not committed yet
        return await real_find_one(*args, **kwargs)

    monkeypatch.setattr(AsyncStep, "find_one", classmethod(miss_once))
    # Make the insert lose: the row is already there.
    original_save = AsyncStep.save

    async def lose(_self):  # noqa: ANN001
        raise IntegrityError("duplicate key value violates unique constraint")

    monkeypatch.setattr(AsyncStep, "save", lose)

    stored = await AsyncStep(job_id=job.id or 0).upsert_from_remote(step_payload)

    monkeypatch.setattr(AsyncStep, "save", original_save)
    assert stored.job_id == job.id
    assert stored.number == 1
    assert await AsyncStep.query().where(AsyncStep.c.job_id == job.id).count() == 1
