# tests/models/test_step_log.py
"""The step log: one row per step, and its absence is a normal answer.

GitHub's run archive carries one file per step for roughly an hour after the run
finishes, then rewrites itself flat. So a step log is something this service
*captures*, never something it can go back for: for an older run the row will
never exist, and code that treats ``None`` as "not fetched yet, retry" would
retry forever.

These tests pin the three properties the rest of the system depends on:

* at most one row per step, replaceable in place (a re-run of the capture must
  not create a sibling row that a later read picks up by accident);
* a step without a capture reads as ``None``, quickly, without touching the
  step row;
* the row keeps the line count and the fetch time, because both are asked far
  more often than the text itself.
"""

from __future__ import annotations

import pytest
from conftest import FakeGithub, subscribe
from rhosocial.activerecord.backend.errors import IntegrityError

from rhosocial.github_ci_tracker.models import (
    AsyncJob,
    AsyncStep,
    AsyncStepLog,
    AsyncWorkflowRun,
)

REPO = "acme/demo"
RUN_ID = 607000


async def _steps() -> tuple[AsyncJob, AsyncStep]:
    """A stored job with one stored step, the way the read path creates them."""
    fake = FakeGithub()
    # An anonymous subscription only covers a public repository.
    fake.repo_info = {
        "id": 77,
        "owner": {"login": "acme"},
        "name": "public",
        "full_name": REPO,
        "is_private": False,
        "default_branch": "main",
    }
    fake.runs = [
        {
            "id": RUN_ID,
            "run_number": 1,
            "run_attempt": 1,
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "head_branch": "main",
            "head_sha": "a" * 40,
            "run_started_at": "2026-10-27T10:00:00Z",
            "completed_at": "2026-10-27T10:05:00Z",
            "workflow_id": 7,
            "path": ".github/workflows/ci.yml",
            "name": "CI",
        }
    ]
    fake.jobs = [
        {
            "id": 900,
            "run_id": RUN_ID,
            "name": "build",
            "status": "completed",
            "conclusion": "failure",
            "runner_name": "ubuntu-latest",
            "started_at": "2026-10-27T10:00:00Z",
            "completed_at": "2026-10-27T10:05:00Z",
            "steps": [
                {
                    "number": 1,
                    "name": "Set up job",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "2026-10-27T10:00:00Z",
                    "completed_at": "2026-10-27T10:00:02Z",
                },
                {
                    "number": 2,
                    "name": "Run tests",
                    "status": "completed",
                    "conclusion": "failure",
                    "started_at": "2026-10-27T10:00:02Z",
                    "completed_at": "2026-10-27T10:04:00Z",
                },
            ],
        }
    ]
    repo = await subscribe(fake)
    run = await AsyncWorkflowRun.adopt_from_remote(repo, fake.runs[0])
    job = await AsyncJob.upsert_from_remote(run, fake.jobs[0])
    steps = await job.steps()
    return job, next(step for step in steps if step.number == 2)


class TestTheOptionalSideIsAbsence:
    async def test_a_step_without_a_capture_reads_as_none(self, bound) -> None:
        _, step = await _steps()

        assert step.id is not None
        assert await AsyncStepLog.find_for_step(step.id) is None

    async def test_a_capture_attaches_to_its_step(self, bound) -> None:
        _, step = await _steps()
        log = AsyncStepLog(step_id=step.id or 0, log_content="line one\nline two\n")

        await log.store()

        stored = await AsyncStepLog.find_for_step(step.id or 0)
        assert stored is not None
        assert stored.log_content == "line one\nline two\n"
        assert stored.line_count == 2, "counted at write time, not by reading it back"
        assert stored.fetched_at is not None

    async def test_the_step_can_reach_its_log(self, bound) -> None:
        _, step = await _steps()
        await AsyncStepLog(step_id=step.id or 0, log_content="hello\n").store()

        attached = await step.step_log()

        assert attached is not None
        assert attached.log_content == "hello\n"


class TestOneRowPerStep:
    async def test_a_second_store_replaces_rather_than_duplicates(self, bound) -> None:
        """Capturing twice (a retry, a re-run) must not leave two rows."""
        _, step = await _steps()
        step_id = step.id or 0
        await AsyncStepLog(step_id=step_id, log_content="first\n").store()
        first = await AsyncStepLog.find_for_step(step_id)

        await AsyncStepLog(step_id=step_id, log_content="second\nthird\n").store()

        rows = await AsyncStepLog.for_step(step_id).all()
        assert len(rows) == 1
        assert rows[0].id == (first.id if first else None), "the row is updated in place"
        assert rows[0].line_count == 2

    async def test_a_second_row_is_refused_by_the_table(self, bound) -> None:
        """``store()`` replaces; a plain ``save()`` cannot create a sibling row.

        The 1:1 is a UNIQUE constraint, not a convention: a future write path
        that forgets to go through ``store()`` fails loudly instead of leaving
        two rows for one step, where a read would pick whichever came first.
        """
        _, step = await _steps()
        await AsyncStepLog(step_id=step.id or 0, log_content="first\n").save()

        with pytest.raises(IntegrityError):
            await AsyncStepLog(step_id=step.id or 0, log_content="second\n").save()

    async def test_the_count_of_captured_steps_is_one_query(self, bound) -> None:
        job, step = await _steps()
        steps = await job.steps()
        await AsyncStepLog(step_id=step.id or 0, log_content="x\n").store()

        ids = [item.id or 0 for item in steps]
        assert await AsyncStepLog.stored_count_for_steps(ids) == 1
        assert await AsyncStepLog.stored_count_for_steps([]) == 0


class TestTheTextIsOurs:
    async def test_lines_strip_the_bom_github_puts_at_the_start(self, bound) -> None:
        _, step = await _steps()
        log = AsyncStepLog(step_id=step.id or 0, log_content="\ufeff2026-10-27T10:00:00Z hello\n")
        await log.store()

        stored = await AsyncStepLog.find_for_step(step.id or 0)

        assert stored is not None
        assert (await stored.lines())[0] == "2026-10-27T10:00:00Z hello"

    async def test_an_empty_capture_is_zero_lines_not_one(self, bound) -> None:
        _, step = await _steps()
        await AsyncStepLog(step_id=step.id or 0, log_content="").store()

        stored = await AsyncStepLog.find_for_step(step.id or 0)

        assert stored is not None
        assert stored.line_count == 0
        assert await stored.lines() == []
