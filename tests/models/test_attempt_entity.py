# tests/models/test_attempt_entity.py
"""The attempt entity: what it stores, and what it is for.

A re-run reuses the run id and increments the attempt number, so the run id
alone never identifies one execution. Measured on a public run with two
attempts: attempt 1 started at ``07:32:41Z`` while the run row had already been
overwritten with attempt 2's ``08:55:41Z``. Before this table existed, storing
the run overwrote the earlier execution's state and the only thing telling the
two apart was an integer on ``ct_jobs``.

The point of these tests is that the earlier attempt's state *survives* -- which
is the whole reason the table exists.
"""

from __future__ import annotations

from conftest import FakeGithub, subscribe

from rhosocial.github_ci_tracker.models import (
    AsyncJob,
    AsyncRunAttempt,
    AsyncWorkflowRun,
)

RUN_ID = 606000


def _client() -> FakeGithub:
    """A client for a publicly readable repository: anonymous subscribes work."""
    fake = FakeGithub()
    fake.repo_info = {
        "id": 42,
        "owner": {"login": "acme"},
        "name": "public",
        "full_name": "acme/public",
        "is_private": False,
        "default_branch": "main",
    }
    return fake


def _run_payload(*, attempt: int = 1) -> dict:
    return {
        "id": RUN_ID,
        "run_number": 7,
        "run_attempt": attempt,
        "name": "CI",
        "path": ".github/workflows/ci.yml",
        "workflow_id": 900,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "head_branch": "main",
        "head_sha": "a" * 40,
        "run_started_at": "2026-10-26T07:32:41Z",
        "created_at": "2026-10-26T07:32:40Z",
        "updated_at": "2026-10-26T07:47:33Z",
        "html_url": f"https://github.com/acme/demo/actions/runs/{RUN_ID}",
        "jobs_url": "",
        "logs_url": "",
        "actor": {"login": "alice"},
    }


def _job_payload(job_id: int, attempt: int, name: str, conclusion: str = "success") -> dict:
    return {
        "id": job_id,
        "run_id": RUN_ID,
        "run_attempt": attempt,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
    }


async def test_each_attempt_keeps_its_own_state(bound) -> None:
    """A re-run must not overwrite attempt 1 with attempt 2's timestamps."""
    repo = await subscribe(_client())
    run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(attempt=1))

    # Attempt 2 arrives as a *new payload for the same run id*.
    later = dict(_run_payload(attempt=2))
    later["run_started_at"] = "2026-10-26T08:55:41Z"
    later["created_at"] = "2026-10-26T08:55:43Z"
    later["updated_at"] = "2026-10-26T08:59:52Z"
    await run.update_from_remote(later)

    first = await AsyncRunAttempt.find_attempt(run.id or 0, 1)
    second = await AsyncRunAttempt.find_attempt(run.id or 0, 2)
    assert first is not None and second is not None

    # Both survive, each with its own clock -- the row that used to be clobbered.
    assert first.started_at.isoformat().startswith("2026-10-26T07:32:41")
    assert second.started_at.isoformat().startswith("2026-10-26T08:55:41")
    assert first.github_updated_at.isoformat().startswith("2026-10-26T07:47:33")
    assert second.github_updated_at.isoformat().startswith("2026-10-26T08:59:52")


async def test_the_run_row_reports_the_latest_attempt(bound) -> None:
    """The newest execution is the highest stored attempt number."""
    repo = await subscribe(_client())
    run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(attempt=1))

    later = dict(_run_payload(attempt=3))
    later["status"] = "completed"
    await run.update_from_remote(later)

    assert run.latest_attempt == 3
    stored = await AsyncWorkflowRun.find_by_github_id(RUN_ID, derived=True)
    assert stored is not None and stored.latest_attempt == 3
    assert (await AsyncRunAttempt.latest_for_run(stored.id or 0)).attempt_number == 3


async def test_jobs_of_two_attempts_stay_separate(bound) -> None:
    """Job ids differ per attempt, so both sets coexist -- and never mix."""
    repo = await subscribe(_client())
    run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(attempt=1))
    await run.sync_jobs_from_remote([_job_payload(1001, 1, "first-attempt", "failure")])

    later = dict(_run_payload(attempt=2))
    await run.update_from_remote(later)
    await run.sync_jobs_from_remote([_job_payload(2001, 2, "second-attempt", "success")])

    assert [job.name for job in await run.jobs_of_attempt()] == ["second-attempt"]
    assert [job.name for job in await run.jobs_of_attempt(1)] == ["first-attempt"]

    # History is preserved: the earlier attempt's jobs are still there.
    every = [job for attempt in await run.attempts() for job in await attempt.jobs()]
    assert sorted(job.github_job_id for job in every) == [1001, 2001]

    # ...and each job knows its own attempt, which is the only thing that keeps
    # the two apart once the run id has been reused.
    by_github_id = {job.github_job_id: job for job in every}
    first_attempt = await AsyncRunAttempt.find_attempt(run.id or 0, 1)
    second_attempt = await AsyncRunAttempt.find_attempt(run.id or 0, 2)
    assert by_github_id[1001].attempt_id == first_attempt.id
    assert by_github_id[2001].attempt_id == second_attempt.id


async def test_a_job_fragment_creates_the_attempt_it_names(bound) -> None:
    """A ``workflow_job`` fragment can arrive for an attempt we never stored.

    The foreign key is NOT NULL, so ``upsert_from_remote`` cannot file the job
    under an attempt row that does not exist yet -- it creates a minimal one and
    the fuller payload fills it in later.
    """
    repo = await subscribe(_client())
    run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(attempt=1))

    fragment = _job_payload(3001, 1, "out-of-order")
    del fragment["run_id"]  # webhook fragments do not always carry it
    job = await AsyncJob.upsert_from_remote(run, fragment)

    attempt = await AsyncRunAttempt.find_one(AsyncRunAttempt.c.id == job.attempt_id)
    assert attempt is not None
    assert attempt.attempt_number == 1
    assert attempt.run_id == run.id
