# tests/core/test_webhook_idempotence.py
"""Applying the same delivery twice leaves one copy of everything.

Two things went wrong in production, and both are answered by the same property:

* ``queued`` and ``in_progress`` for the same job arrive a second apart, so two
  handlers reach the same rows at once;
* a delivery that stopped halfway used to leave a job row with no steps, which
  looks complete: the row exists, nothing retries it, and the gap only shows up
  when someone compares the database with GitHub.

The receiver does not try to apply a delivery atomically -- that is the
unit-of-work idea this project rejects, and each model write is complete on its
own. What it does guarantee is that every step is an **upsert keyed by the
GitHub id**, so applying a delivery twice, or applying the next state of the
same job, updates the rows already there instead of inserting rivals. A delivery
that stops halfway leaves a partial state that the next read repairs, because
GitHub is the source for everything that can change.
"""

from __future__ import annotations

from conftest import FakeGithub, make_webhook_service

from rhosocial.github_ci_tracker.models import (
    AsyncJob,
    AsyncRepository,
    AsyncStep,
    AsyncWorkflow,
    AsyncWorkflowRun,
)

REPO = "acme/demo"
RUN_ID = 6001
JOB_ID = 7001


def _remote_run() -> dict:
    return {
        "id": RUN_ID,
        "run_number": 1,
        "run_attempt": 1,
        "status": "queued",
        "conclusion": None,
        "head_branch": "main",
        "head_sha": "c" * 40,
        "event": "push",
        "workflow_id": 11,
        "path": ".github/workflows/test.yml",
        "name": "CI",
    }


def _job_payload(action: str, status: str) -> dict:
    return {
        "action": action,
        "repository": {"id": 3, "full_name": REPO},
        "workflow_job": {
            "id": JOB_ID,
            "run_id": RUN_ID,
            "name": "tests",
            "status": status,
            "conclusion": None,
            "steps": [
                {"number": 1, "name": "checkout", "status": status},
                {"number": 2, "name": "run tests", "status": status},
            ],
        },
    }


async def _subscribe() -> AsyncRepository:
    owner, _, name = REPO.partition("/")
    repo = AsyncRepository(github_repo_id=41, full_name=REPO, owner=owner, name=name)
    await repo.save()
    return repo


class TestRepeatedDelivery:
    async def test_queued_then_in_progress_applies_both_without_error(self, bound) -> None:
        await _subscribe()
        handler = make_webhook_service(client=FakeGithub(runs=[_remote_run()]))

        assert await handler.handle_event("workflow_job", _job_payload("queued", "queued"))
        assert await handler.handle_event(
            "workflow_job", _job_payload("in_progress", "in_progress")
        )

        job = await AsyncJob.find_by_github_id(JOB_ID)
        assert job is not None
        assert job.status == "in_progress"
        # Both deliveries carried the same two steps; the second must update
        # them, not insert a second pair.
        assert await AsyncStep.query().where(AsyncStep.c.job_id == job.id).count() == 2

    async def test_the_same_delivery_twice_is_one_run_one_job(self, bound) -> None:
        await _subscribe()
        handler = make_webhook_service(client=FakeGithub(runs=[_remote_run()]))
        payload = _job_payload("queued", "queued")

        await handler.handle_event("workflow_job", payload)
        await handler.handle_event("workflow_job", payload)

        assert await AsyncWorkflowRun.query().count() == 1
        assert await AsyncJob.query().count() == 1
        assert await AsyncStep.query().count() == 2

    async def test_a_workflow_inserted_twice_keeps_one_row(self, bound) -> None:
        """The run's workflow is upserted by the same delivery; it must not race itself."""
        repo = await _subscribe()

        first = await AsyncWorkflow(
            repository_id=repo.id or 0, workflow_id=11, name="CI", path="a.yml"
        ).upsert_from_remote({"id": 11, "name": "CI", "path": "a.yml"})
        second = await AsyncWorkflow(
            repository_id=repo.id or 0, workflow_id=11, name="CI", path="a.yml"
        ).upsert_from_remote({"id": 11, "name": "CI", "path": "a.yml"})

        assert second.id == first.id
        assert await AsyncWorkflow.query().count() == 1
