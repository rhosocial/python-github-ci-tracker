# tests/controllers/test_dispatch_controller.py
"""End to end for ``DispatchController``: fake transport, real models, real database.

The stack is the one a protocol adapter will use:

    DispatchController -> RunControl / RunReader (GithubRuns) -> transport -> models -> database

Only the transport is faked. Everything below it is real: workflow resolution,
the adopt path, the re-run's attempt row, and the state machine.

The service is typed against the two protocols, so it holds a reader and a
writer and nothing else -- there is no client, no token and no policy object to
reach for.
"""

from __future__ import annotations

import pytest
from conftest import FakeGithub

from rhosocial.github_ci_tracker.controllers import RunData
from rhosocial.github_ci_tracker.controllers.dispatch import (
    DispatchController,
    _dispatch_identifier,
)
from rhosocial.github_ci_tracker.controllers.run import RunController
from rhosocial.github_ci_tracker.controllers.trigger import TriggerController
from rhosocial.github_ci_tracker.core.exceptions import (
    GithubAPIError,
    RepositoryNotFoundError,
    RunInProgressError,
    RunNotFoundError,
    TokenPermissionError,
    TriggerFailedError,
    ValidationError,
)
from rhosocial.github_ci_tracker.core.run_tracker import RunTracker
from rhosocial.github_ci_tracker.models import AsyncJob, AsyncRepository, AsyncWorkflow
from rhosocial.github_ci_tracker.models.attempt import AsyncRunAttempt
from rhosocial.github_ci_tracker.models.errors import WorkflowNotFoundError
from rhosocial.github_ci_tracker.models.run import AsyncWorkflowRun
from rhosocial.github_ci_tracker.services.github import GithubRuns
from rhosocial.github_ci_tracker.services.github.protocols import RunControl, RunReader

REPO = "acme/demo"
RUN_ID = 500
WORKFLOW_PATH = ".github/workflows/ci.yml"


async def _tracked(full_name: str = REPO) -> AsyncRepository:
    """Store a repository row and the one workflow the tests dispatch."""
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


def _service(fake: FakeGithub, tracker: RunTracker | None = None) -> DispatchController:
    runs = GithubRuns(fake)
    return DispatchController(control=runs, readers=runs, runs_data=RunData(), tracker=tracker)


def _trigger(fake: FakeGithub) -> TriggerController:
    """The protocol-facing write path over the same fake transport."""
    runs = GithubRuns(fake)
    return TriggerController(
        dispatch=DispatchController(control=runs, readers=runs, runs_data=RunData()),
        runs=RunController(runs=runs, runs_data=RunData()),
    )


class TestDispatchIdentifier:
    """The dispatch endpoint takes a file name, not a repository path."""

    def test_a_path_becomes_a_file_name(self) -> None:
        workflow = AsyncWorkflow(workflow_id=7, name="CI", path=".github/workflows/ci.yml")
        assert _dispatch_identifier(workflow) == "ci.yml"

    def test_a_bare_name_is_kept(self) -> None:
        workflow = AsyncWorkflow(workflow_id=7, name="CI", path="ci.yml")
        assert _dispatch_identifier(workflow) == "ci.yml"

    def test_a_missing_path_falls_back_to_the_numeric_id(self) -> None:
        workflow = AsyncWorkflow(workflow_id=12345, name="CI", path="")
        assert _dispatch_identifier(workflow) == "12345"


class TestTheGroups:
    def test_one_object_satisfies_both_halves(self) -> None:
        """The split is a property of the protocols, not of the implementation."""
        runs = GithubRuns(FakeGithub())

        assert isinstance(runs, RunControl)
        assert isinstance(runs, RunReader)


class TestDispatch:
    async def test_a_204_dispatch_is_resolved_from_the_runs_list(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()

        run = await _service(fake).dispatch(REPO, "ci.yml", "main")

        assert fake.trigger_calls == 1
        # GitHub answered 204, so the created run was found by reading the list
        # back and adopting the only run this database did not have.
        assert run.github_run_id == RUN_ID
        assert run.latest_status == "completed"
        assert await AsyncWorkflowRun.query().count() == 1

    async def test_the_dispatch_sends_a_file_name_not_a_path(self, bound) -> None:
        """``POST .../dispatches`` 404s on a path; the caller's spelling is not sent."""
        await _tracked()
        fake = FakeGithub()
        seen: dict[str, str] = {}
        original = fake.trigger_workflow

        async def spy(token, repo_full_name, workflow_id, ref, inputs=None, **kwargs):
            seen["workflow_id"] = workflow_id
            return await original(token, repo_full_name, workflow_id, ref, inputs, **kwargs)

        fake.trigger_workflow = spy  # type: ignore[method-assign]

        await _service(fake).dispatch(REPO, WORKFLOW_PATH, "main")

        assert seen["workflow_id"] == "ci.yml"

    async def test_an_unknown_workflow_never_reaches_github(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()

        with pytest.raises(WorkflowNotFoundError):
            await _service(fake).dispatch(REPO, "nope.yml", "main")

        assert fake.trigger_calls == 0

    async def test_an_untracked_repository_is_refused(self, bound) -> None:
        fake = FakeGithub()

        with pytest.raises(RepositoryNotFoundError):
            await _service(fake).dispatch("nobody/nothing", "ci.yml", "main")

        assert fake.trigger_calls == 0

    async def test_the_tracker_learns_the_runs_this_service_started(self, bound) -> None:
        """Webhook filtering depends on it: a delivery for an unknown run is foreign."""
        await _tracked()
        tracker = RunTracker()

        await _service(FakeGithub(), tracker).dispatch(REPO, "ci.yml", "main")

        assert tracker.is_tracked(RUN_ID)

    async def test_without_a_tracker_nothing_is_recorded(self, bound) -> None:
        """A script that receives no webhooks has no reason to hold the set."""
        await _tracked()

        await _service(FakeGithub()).dispatch(REPO, "ci.yml", "main")

        # Nothing to assert but the absence of an error: the dispatch still
        # stores its run.
        assert await AsyncWorkflowRun.query().count() == 1


class TestRerun:
    async def test_a_rerun_advances_the_attempt_and_keeps_the_run_id(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()
        tracker = RunTracker()
        service = _service(fake, tracker)
        await service.dispatch(REPO, "ci.yml", "main")

        run, previous = await service.rerun(REPO, RUN_ID)

        assert run.github_run_id == RUN_ID
        assert previous == 1
        assert run.latest_attempt == 2
        # A re-run is the same run at a new attempt, never a second row.
        assert await AsyncWorkflowRun.query().count() == 1
        assert fake.rerun_calls == [RUN_ID]
        assert tracker.is_tracked(RUN_ID)

    async def test_a_rerun_of_an_unknown_run_never_reaches_github(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()

        with pytest.raises(RunNotFoundError):
            await _service(fake).rerun(REPO, RUN_ID)

        assert fake.rerun_calls == []

    async def test_the_earlier_attempt_survives_the_rerun(self, bound) -> None:
        """A re-run writes a new attempt row; the earlier execution is untouched."""
        await _tracked()
        fake = FakeGithub()
        service = _service(fake)
        await service.dispatch(REPO, "ci.yml", "main")
        run = await AsyncWorkflowRun.find_by_github_id(RUN_ID)
        assert run is not None

        await service.rerun(REPO, RUN_ID)

        first = await AsyncRunAttempt.find_attempt(run.id or 0, 1)
        second = await AsyncRunAttempt.find_attempt(run.id or 0, 2)
        assert first is not None and first.status == "completed"
        assert first.conclusion == "success"
        assert second is not None and second.status == "queued"

    async def test_a_read_back_failure_is_reported_not_swallowed(self, bound) -> None:
        """GitHub accepted the re-run; a failed read-back says so instead of
        returning the stale row, which would claim the re-run never happened."""
        await _tracked()
        fake = FakeGithub()
        service = _service(fake)
        await service.dispatch(REPO, "ci.yml", "main")

        async def missing(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise GithubAPIError("missing", status_code=404)

        fake.get_run = missing  # type: ignore[method-assign]

        with pytest.raises(TriggerFailedError):
            await service.rerun(REPO, RUN_ID)

        stored = await AsyncWorkflowRun.find_by_github_id(RUN_ID, derived=True)
        assert stored is not None and stored.latest_attempt == 1

    async def test_a_rerun_of_another_repositorys_run_is_refused(self, bound) -> None:
        await _tracked(REPO)
        await _tracked("other/project")
        fake = FakeGithub()
        await _service(fake).dispatch(REPO, "ci.yml", "main")

        with pytest.raises(RunNotFoundError):
            await _service(fake).rerun("other/project", RUN_ID)

        assert fake.rerun_calls == []


class TestRerunAttemptReporting:
    """The caller must be able to confirm which attempt it just started."""

    async def test_a_lagging_read_back_still_reports_the_new_attempt(self, bound) -> None:
        """GitHub accepts the re-run before its own read reflects it."""
        await _tracked()
        fake = FakeGithub()
        service = _service(fake)
        await service.dispatch(REPO, "ci.yml", "main")

        fresh = fake.get_run
        calls = {"count": 0}

        async def lagging(token, repo_full_name, run_id):  # noqa: ANN001, ANN202
            payload = await fresh(token, repo_full_name, run_id)
            calls["count"] += 1
            if calls["count"] == 1:
                payload["run_attempt"] = 1  # the stale answer
            return payload

        fake.get_run = lagging  # type: ignore[method-assign]

        run, previous = await service.rerun(REPO, RUN_ID, resolve_delay=0)

        assert previous == 1
        assert run.latest_attempt == 2
        assert calls["count"] == 2, "the stale read is retried, not believed"

    async def test_a_read_back_that_never_advances_is_reported_unchanged(self, bound) -> None:
        """Bounded retries, then the truth: the attempt did not move."""
        await _tracked()
        fake = FakeGithub()
        service = _service(fake)
        await service.dispatch(REPO, "ci.yml", "main")

        fresh = fake.get_run

        async def stuck(token, repo_full_name, run_id):  # noqa: ANN001, ANN202
            payload = await fresh(token, repo_full_name, run_id)
            payload["run_attempt"] = 1
            return payload

        fake.get_run = stuck  # type: ignore[method-assign]

        run, previous = await service.rerun(REPO, RUN_ID, resolve_attempts=2, resolve_delay=0)

        assert previous == 1
        assert run.latest_attempt == 1, "no invented attempt number"

    async def test_rerunning_a_running_run_is_a_concurrency_error(self, bound) -> None:
        """GitHub's 403 here is about the run, not the token."""
        await _tracked()
        fake = FakeGithub()
        service = _service(fake)
        await service.dispatch(REPO, "ci.yml", "main")

        async def already_running(token, repo_full_name, run_id):  # noqa: ANN001, ANN202
            raise GithubAPIError("This workflow is already running", status_code=403)

        fake.rerun_workflow = already_running  # type: ignore[method-assign]

        with pytest.raises(RunInProgressError, match="already running"):
            await service.rerun(REPO, RUN_ID)


class TestCancel:
    async def test_the_state_is_re_read_after_the_cancel(self, bound) -> None:
        """GitHub answers 202 before the runner stops, so the answer is not assumed."""
        await _tracked()
        fake = FakeGithub()
        service = _service(fake)
        await service.dispatch(REPO, "ci.yml", "main")
        # What GitHub reports a moment after the cancellation is accepted.
        fake.runs[0] = {**fake.runs[0], "status": "completed", "conclusion": "cancelled"}

        run = await service.cancel(REPO, RUN_ID)

        assert fake.cancel_calls == [RUN_ID]
        assert run.latest_status == "completed"
        assert run.latest_conclusion == "cancelled"

    async def test_cancelling_an_unknown_run_never_reaches_github(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()

        with pytest.raises(RunNotFoundError):
            await _service(fake).cancel(REPO, RUN_ID)

        assert fake.cancel_calls == []


class TestWait:
    """``wait_for_completion`` is a sequence of reads, not a second source of truth."""

    async def test_a_finished_run_costs_two_reads_and_returns_its_jobs(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()
        trigger = _trigger(fake)

        view = await trigger.wait_for_completion(REPO, RUN_ID)

        assert view.completed is True
        assert view.conclusion == "success"
        # The opening read adopts the run, the closing one stores its jobs.
        assert fake.run_calls == [RUN_ID, RUN_ID]
        assert await AsyncJob.query().count() == 1

    async def test_a_timeout_is_not_a_failure(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()
        fake.runs[0] = {**fake.runs[0], "status": "in_progress", "conclusion": None}
        trigger = _trigger(fake)

        # The budget has to survive one opening read on a slow database -- that
        # read writes a row -- otherwise the deadline is already spent by the
        # time the first check runs and the loop proves nothing about itself.
        view = await trigger.wait_for_completion(REPO, RUN_ID, timeout=5.0, poll_interval=0.01)

        assert view.completed is False
        assert view.status == "in_progress"
        # It gave up instead of spinning forever, and it kept reading the run.
        assert len(fake.run_calls) >= 2


class TestIfAbsent:
    """``if_absent`` answers "this commit has run", not "start another run"."""

    async def test_a_run_for_the_head_is_returned_and_nothing_is_dispatched(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()
        trigger = _trigger(fake)

        result = await trigger.trigger_workflow(REPO, "ci.yml", "main", if_absent=True)

        assert fake.trigger_calls == 0, "no CI minutes were spent"
        assert result.run_id == RUN_ID
        assert result.dispatched is False
        assert "nothing was dispatched" in result.message

    async def test_a_run_for_an_older_commit_does_not_count(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()
        # The stored run is for ``abc``; the branch has moved on to ``def456``.
        fake.ref_shas["main"] = "def456"

        result = await _trigger(fake).trigger_workflow(REPO, "ci.yml", "main", if_absent=True)

        assert fake.trigger_calls == 1, "that commit had not run, so it was dispatched"
        assert result.dispatched is True

    async def test_a_known_sha_is_checked_without_reading_the_ref(self, bound) -> None:
        """An Actions-only PAT cannot read branches; the caller's sha avoids that."""
        await _tracked()
        fake = FakeGithub()

        result = await _trigger(fake).trigger_workflow(
            REPO, "ci.yml", "main", if_absent=True, commit_sha="abc"
        )

        assert fake.ref_calls == []
        assert result.dispatched is False
        assert result.run_id == RUN_ID

    async def test_an_unreadable_ref_asks_for_the_sha_instead_of_dispatching(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()

        async def forbidden(token, repo_full_name, ref):  # noqa: ANN001, ANN202
            raise TokenPermissionError(
                "Insufficient token permissions: Resource not accessible by personal access token"
            )

        fake.get_ref_sha = forbidden  # type: ignore[method-assign]

        with pytest.raises(ValidationError) as caught:
            await _trigger(fake).trigger_workflow(REPO, "ci.yml", "main", if_absent=True)

        assert "commit_sha" in str(caught.value)
        assert fake.trigger_calls == 0, "an unanswerable check must not spend a run"

    async def test_the_default_still_dispatches(self, bound) -> None:
        await _tracked()
        fake = FakeGithub()

        result = await _trigger(fake).trigger_workflow(REPO, "ci.yml", "main")

        assert fake.trigger_calls == 1
        assert result.dispatched is True

    async def test_a_run_of_another_workflow_on_the_same_sha_does_not_count(self, bound) -> None:
        """Two workflows share a sha; only the requested one makes it "already run"."""
        await _tracked()
        # An unresolved local row (``workflow_id = 0``) is the case where the
        # server-side filter cannot help: every workflow's runs come back, and
        # the decision has to be made on the path.
        workflow = await AsyncWorkflow.find_one(AsyncWorkflow.c.repository_id == 1)
        assert workflow is not None
        workflow.workflow_id = 0
        await workflow.save()
        fake = FakeGithub()
        fake.runs = [
            {
                **fake.runs[0],
                "id": 502,
                "path": ".github/workflows/publish.yml",
                "name": "Publish",
                "workflow_id": 8,
            }
        ]

        await _trigger(fake).trigger_workflow(REPO, "ci.yml", "main", if_absent=True)

        assert fake.trigger_calls == 1
