# tests/controllers/test_run_controller.py
"""End to end for ``RunController``: fake transport, real models, real database.

The stack is the one a protocol adapter will use:

    RunController -> RunReader (GithubRuns) -> transport -> models -> database

Only the transport is faked, at the seam the rest of the suite already fakes
(``conftest.FakeGithub`` implements it). Everything below is real: the adopt-or-
update path, the state machine, the attempt rows, the job and step cascade.

``RunController`` is typed against ``RunReader``, not against ``GithubRuns``, so
nothing here can dispatch -- that is a compile-time property, not a convention.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakeGithub

from rhosocial.github_ci_tracker.controllers import RunData
from rhosocial.github_ci_tracker.controllers.run import RunController
from rhosocial.github_ci_tracker.core.exceptions import (
    GithubAPIError,
    RepositoryNotFoundError,
    RunNotFoundError,
)
from rhosocial.github_ci_tracker.models import AsyncRepository
from rhosocial.github_ci_tracker.models.attempt import AsyncRunAttempt
from rhosocial.github_ci_tracker.models.job import AsyncJob
from rhosocial.github_ci_tracker.models.run import AsyncWorkflowRun
from rhosocial.github_ci_tracker.models.step import AsyncStep
from rhosocial.github_ci_tracker.services.github import GithubRuns

REPO = "acme/demo"
RUN_ID = 500


async def _tracked(full_name: str = REPO) -> AsyncRepository:
    """Store a repository row, as subscribing would have done."""
    owner, name = full_name.split("/", 1)
    repo = AsyncRepository(
        github_repo_id=hash(full_name) % 100_000,
        full_name=full_name,
        owner=owner,
        name=name,
    )
    await repo.save()
    return repo


def _service(fake: FakeGithub) -> RunController:
    return RunController(runs=GithubRuns(fake), runs_data=RunData())


class TestFetch:
    async def test_adopts_an_unknown_run_with_its_jobs_and_steps(self, bound) -> None:
        repo = await _tracked()
        fake = FakeGithub()

        run = await _service(fake).fetch(repo, RUN_ID)

        assert run.github_run_id == RUN_ID
        assert run.latest_status == "completed"
        assert run.latest_conclusion == "success"

        # The run, its attempt, its job and the job's step all exist.
        assert len(await run.attempt_ids()) == 1
        assert await AsyncJob.query().count() == 1
        assert await AsyncStep.query().count() == 1

    async def test_a_second_fetch_updates_instead_of_duplicating(self, bound) -> None:
        repo = await _tracked()
        fake = FakeGithub()
        service = _service(fake)

        first = await service.fetch(repo, RUN_ID)
        second = await service.fetch(repo, RUN_ID)

        assert second.id == first.id
        assert await AsyncWorkflowRun.query().count() == 1
        assert fake.run_calls == [RUN_ID, RUN_ID]

    async def test_a_rerun_advances_the_attempt_and_stores_a_new_row(self, bound) -> None:
        repo = await _tracked()
        fake = FakeGithub()
        service = _service(fake)
        await service.fetch(repo, RUN_ID)

        # What GitHub reports after a re-run: same id, attempt 2, queued again.
        fake.runs[0] = {**fake.runs[0], "run_attempt": 2, "status": "queued", "conclusion": None}

        run = await service.fetch(repo, RUN_ID)

        assert run.latest_attempt == 2
        # A new attempt is a new execution: its own row exists, and the jobs
        # hang off that one rather than off the previous execution.
        assert len(await run.attempt_ids()) == 2
        assert await AsyncRunAttempt.query().count() == 2

    async def test_jobs_can_be_skipped_when_only_the_state_is_wanted(self, bound) -> None:
        repo = await _tracked()
        fake = FakeGithub()

        await _service(fake).fetch(repo, RUN_ID, with_jobs=False)

        assert await AsyncJob.query().count() == 0

    async def test_a_transport_error_is_not_swallowed(self, bound) -> None:
        repo = await _tracked()
        fake = FakeGithub(runs=[])

        with pytest.raises(GithubAPIError):
            await _service(fake).fetch(repo, RUN_ID)


class TestGet:
    async def test_an_unknown_run_is_adopted_rather_than_reported_missing(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()

        run = await _service(fake).get(REPO, RUN_ID)

        assert run.github_run_id == RUN_ID
        assert fake.run_calls == [RUN_ID]

    async def test_a_known_run_is_answered_from_the_cache(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()
        service = _service(fake)
        await service.fetch_by_name(REPO, RUN_ID)

        run = await service.get(REPO, RUN_ID)

        assert run.github_run_id == RUN_ID
        # Exactly the one read the fetch made: answering a known run must not
        # spend a GitHub call.
        assert fake.run_calls == [RUN_ID]

    async def test_refresh_ignores_the_cache(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()
        service = _service(fake)
        await service.fetch_by_name(REPO, RUN_ID)

        await service.get(REPO, RUN_ID, refresh=True)

        assert fake.run_calls == [RUN_ID, RUN_ID]

    async def test_an_untracked_repository_is_an_error(self, bound) -> None:
        fake = FakeGithub()

        with pytest.raises(RepositoryNotFoundError):
            await _service(fake).get("nobody/nothing", RUN_ID)

        assert fake.run_calls == []

    async def test_a_run_of_another_repository_is_refused(self, bound) -> None:
        await _tracked(REPO)
        await _tracked("other/project")
        fake = FakeGithub()
        service = _service(fake)

        await service.fetch_by_name(REPO, RUN_ID)

        # The run is stored and belongs to acme/demo; asking for it through
        # other/project must not answer with it.
        with pytest.raises(RunNotFoundError):
            await service.get("other/project", RUN_ID)


class TestDurationSample:
    """The wait budget comes from finished runs of the same workflow."""

    @staticmethod
    def _payload(
        run_id: int, *, starts_after: int, minutes: int, status: str = "completed"
    ) -> dict:
        """A run payload whose execution starts ``starts_after`` minutes in."""
        base = datetime(2026, 10, 30, tzinfo=timezone.utc) + timedelta(minutes=starts_after)
        started = base.strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "id": run_id,
            "run_number": 1,
            "run_attempt": 1,
            "event": "push",
            "status": status,
            "conclusion": "success" if status == "completed" else None,
            "head_branch": "main",
            "head_sha": "a" * 40,
            "run_started_at": started,
            "completed_at": (
                (base + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
                if status == "completed"
                else None
            ),
            "workflow_id": 7,
            "path": ".github/workflows/ci.yml",
            "name": "CI",
        }

    async def _adopt(self, runs: list[dict]) -> int:
        from rhosocial.github_ci_tracker.models import AsyncWorkflow

        repo = await _tracked()
        workflow = None
        for payload in runs:
            run = await AsyncWorkflowRun.adopt_from_remote(repo, payload)
            workflow = await AsyncWorkflow.find_one(AsyncWorkflow.c.id == run.workflow_id)
        assert workflow is not None and workflow.id is not None
        return workflow.id

    async def test_percentiles_come_from_the_finished_runs(self, bound) -> None:
        workflow_id = await self._adopt(
            [
                self._payload(501, starts_after=0, minutes=2),
                self._payload(502, starts_after=10, minutes=6),
                self._payload(503, starts_after=20, minutes=12),
                # Still running: its "end" is now, which would drift the sample.
                self._payload(504, starts_after=30, minutes=0, status="in_progress"),
            ]
        )

        sample = await _service(FakeGithub()).duration_sample(workflow_id)

        assert sample is not None
        assert sample.samples == 3
        assert sample.p50_seconds == 360
        assert sample.p90_seconds == 720

    async def test_a_fresh_workflow_has_no_sample(self, bound) -> None:
        repo = await _tracked()
        from rhosocial.github_ci_tracker.models import AsyncWorkflow

        workflow = AsyncWorkflow(
            repository_id=repo.id or 0,
            workflow_id=7,
            name="CI",
            path=".github/workflows/ci.yml",
        )
        await workflow.save()

        assert await _service(FakeGithub()).duration_sample(workflow.id or 0) is None

    async def test_a_running_run_only_workflow_has_no_sample(self, bound) -> None:
        workflow_id = await self._adopt(
            [self._payload(505, starts_after=30, minutes=0, status="in_progress")]
        )

        assert await _service(FakeGithub()).duration_sample(workflow_id) is None
