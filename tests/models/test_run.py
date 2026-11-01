# tests/models/test_run.py
"""AsyncWorkflowRun: adoption, remote updates and the query scopes.

The run model is the mirror of a GitHub Actions execution. ``adopt_from_remote``
stores a run the system has never seen (upserting its workflow first),
``update_from_remote`` applies remote payloads through the forward-only state
machine, and the query domain (``recent``, ``latest_by_branch``,
``latest_per_workflow``, ``status_summary``) reads the local mirror only.
"""

from __future__ import annotations

from typing import Any

import pytest

from rhosocial.github_ci_tracker.models import (
    AsyncJob,
    AsyncRepository,
    AsyncWorkflowRun,
)


async def _make_repo(full_name: str = "acme/demo") -> AsyncRepository:
    """Persist a subscription row directly (no GitHub round trip needed)."""
    owner, _, name = full_name.partition("/")
    repo = AsyncRepository(
        github_repo_id=99,
        full_name=full_name,
        owner=owner or "acme",
        name=name or "demo",
        default_branch="main",
    )
    await repo.save()
    return repo


def _run_payload(
    github_run_id: int,
    run_number: int,
    *,
    workflow_id: int = 7,
    path: str = ".github/workflows/ci.yml",
    name: str = "CI",
    status: str = "queued",
    conclusion: str | None = None,
    branch: str = "main",
    event: str = "workflow_dispatch",
    started_key: str = "run_started_at",
    started_at: str | None = "2026-10-10T10:00:00Z",
) -> dict[str, Any]:
    """Build a GitHub run payload (REST object spelling by default)."""
    payload: dict[str, Any] = {
        "id": github_run_id,
        "run_number": run_number,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "head_branch": branch,
        "head_sha": f"sha-{github_run_id}",
        "head_commit": {"message": f"message {github_run_id}"},
        "actor": {"login": "alice"},
        "workflow_id": workflow_id,
        "path": path,
        "name": name,
    }
    if started_at is not None:
        payload[started_key] = started_at
    if status == "completed":
        payload["completed_at"] = "2026-10-10T10:05:00Z"
    return payload


async def _seed_two_workflows(repo: AsyncRepository) -> tuple[int, int]:
    """Adopt three runs of workflow 7 and one of workflow 8; return local ids."""
    await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(501, 1))
    await AsyncWorkflowRun.adopt_from_remote(
        repo, _run_payload(502, 2, status="in_progress", branch="feature")
    )
    await AsyncWorkflowRun.adopt_from_remote(
        repo, _run_payload(503, 3, status="completed", conclusion="success")
    )
    await AsyncWorkflowRun.adopt_from_remote(
        repo,
        _run_payload(
            510,
            10,
            workflow_id=8,
            path=".github/workflows/deploy.yml",
            name="Deploy",
            status="completed",
            conclusion="failure",
        ),
    )
    workflows = await repo.workflows()
    assert len(workflows) == 2
    return workflows[0].id or 0, workflows[1].id or 0


class TestAdoptFromRemote:
    async def test_stores_the_run_and_upserts_its_workflow(self, bound) -> None:
        repo = await _make_repo()

        run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(500, 1))

        assert run.id is not None
        assert run.github_run_id == 500
        assert run.run_number == 1
        assert run.event == "workflow_dispatch"
        assert run.branch == "main"
        assert run.commit_sha == "sha-500"
        workflows = await repo.workflows()
        assert [(workflow.workflow_id, workflow.path) for workflow in workflows] == [
            (7, ".github/workflows/ci.yml")
        ]
        assert workflows[0].id == run.workflow_id

    async def test_maps_the_full_payload(self, bound) -> None:
        repo = await _make_repo()

        run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(500, 1))

        assert run.latest_attempt == 1
        assert run.latest_status == "queued"
        assert run.latest_conclusion is None
        assert run.commit_message == "message 500"
        assert run.actor == "alice"
        assert run.latest_started_at is not None
        assert run.latest_started_at.year == 2026
        assert run.latest_completed_at is None  # queued runs have not finished yet

    async def test_normalizes_status_synonyms(self, bound) -> None:
        repo = await _make_repo()

        run = await AsyncWorkflowRun.adopt_from_remote(
            repo, _run_payload(500, 1, status="requested")
        )

        assert run.latest_status == "queued"

    async def test_accepts_the_webhook_started_at_spelling(self, bound) -> None:
        repo = await _make_repo()

        run = await AsyncWorkflowRun.adopt_from_remote(
            repo,
            _run_payload(500, 1, started_key="started_at"),
        )

        assert run.latest_started_at is not None

    async def test_adopted_run_is_found_by_github_id(self, bound) -> None:
        repo = await _make_repo()
        adopted = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(500, 1))

        found = await AsyncWorkflowRun.find_by_github_id(500)

        assert found is not None
        assert found.id == adopted.id


class TestUpdateFromRemote:
    async def test_moves_forward_through_the_state_machine(self, bound) -> None:
        repo = await _make_repo()
        run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(500, 1))

        await run.update_from_remote(_run_payload(500, 1, status="in_progress"))
        stored = await AsyncWorkflowRun.find_by_github_id(500, derived=True)
        assert stored is not None
        assert stored.latest_status == "in_progress"
        assert stored.latest_started_at is not None

        await stored.update_from_remote(
            _run_payload(500, 1, status="completed", conclusion="success")
        )
        final = await AsyncWorkflowRun.find_by_github_id(500, derived=True)
        assert final is not None
        assert final.latest_status == "completed"
        assert final.latest_conclusion == "success"
        assert final.latest_completed_at is not None

    async def test_ignores_out_of_order_status_but_backfills_metadata(self, bound) -> None:
        """A stale payload must not resurrect a completed run (never fatal)."""
        repo = await _make_repo()
        run = await AsyncWorkflowRun.adopt_from_remote(
            repo, _run_payload(500, 1, status="completed", conclusion="success")
        )

        stale = _run_payload(500, 2, status="queued", branch="stale-branch")
        await run.update_from_remote(stale)

        stored = await AsyncWorkflowRun.find_by_github_id(500, derived=True)
        assert stored is not None
        # The run's state is the newest attempt's, and one attempt moves
        # forward only: a "queued" payload arriving after completion is the
        # past, not a regression.
        assert stored.latest_status == "completed"
        assert stored.latest_conclusion == "success"
        assert stored.branch == "stale-branch"  # metadata is filled regardless
        assert stored.run_number == 2

    async def test_same_status_replay_refreshes_the_conclusion(self, bound) -> None:
        repo = await _make_repo()
        run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(500, 1))

        await run.update_from_remote(_run_payload(500, 1, status="completed"))
        correction = _run_payload(500, 1, status="completed", conclusion="failure")
        await run.update_from_remote(correction)

        stored = await AsyncWorkflowRun.find_by_github_id(500, derived=True)
        assert stored is not None
        assert stored.latest_status == "completed"
        assert stored.latest_conclusion == "failure"

    async def test_updates_metadata_without_touching_the_status(self, bound) -> None:
        repo = await _make_repo()
        run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(500, 1))

        updated = _run_payload(500, 3, status="queued", branch="feature-x")
        updated["head_commit"] = {"message": "rebased"}
        await run.update_from_remote(updated)

        stored = await AsyncWorkflowRun.find_by_github_id(500, derived=True)
        assert stored is not None
        assert stored.latest_status == "queued"
        assert stored.run_number == 3
        assert stored.branch == "feature-x"
        assert stored.commit_message == "rebased"


class TestAttemptOwnsTheState:
    async def test_the_attempt_row_holds_status_and_timestamps(self, bound) -> None:
        """The run row has no status column; the attempt is the record of it."""
        from rhosocial.github_ci_tracker.models.attempt import AsyncRunAttempt

        repo = await _make_repo()
        run = await AsyncWorkflowRun.adopt_from_remote(
            repo, _run_payload(500, 1, status="completed", conclusion="success")
        )

        attempt = await AsyncRunAttempt.latest_for_run(run.id or 0)
        assert attempt is not None
        assert (attempt.status, attempt.conclusion) == ("completed", "success")
        assert attempt.completed_at is not None
        assert attempt.is_finished is True


class TestQueryScopes:
    async def test_recent_orders_by_run_number_descending(self, bound) -> None:
        repo = await _make_repo()
        first_id, _ = await _seed_two_workflows(repo)

        runs = await AsyncWorkflowRun.recent([first_id]).all()

        assert [run.run_number for run in runs] == [3, 2, 1]

    async def test_recent_filters_by_branch(self, bound) -> None:
        """There is no status filter: a run row carries no status to filter on."""
        repo = await _make_repo()
        first_id, second_id = await _seed_two_workflows(repo)

        feature = await AsyncWorkflowRun.recent([first_id], branch="feature").all()
        assert [run.run_number for run in feature] == [2]

        both = await AsyncWorkflowRun.recent([first_id, second_id]).all()
        assert [run.run_number for run in both] == [10, 3, 2, 1]  # newest first

    async def test_latest_by_branch(self, bound) -> None:
        repo = await _make_repo()
        first_id, _ = await _seed_two_workflows(repo)

        latest = await AsyncWorkflowRun.latest_by_branch([first_id], "main")
        assert latest is not None and latest.run_number == 3

        feature = await AsyncWorkflowRun.latest_by_branch([first_id], "feature")
        assert feature is not None and feature.run_number == 2

        assert await AsyncWorkflowRun.latest_by_branch([first_id], "ghost") is None
        assert await AsyncWorkflowRun.latest_by_branch([], "main") is None

    async def test_latest_per_workflow_keeps_k_runs_of_one_workflow(self, bound) -> None:
        repo = await _make_repo()
        first_id, _ = await _seed_two_workflows(repo)

        latest = await AsyncWorkflowRun.latest_per_workflow([first_id], k=1)
        assert [run.github_run_id for run in latest] == [503]

        top_two = await AsyncWorkflowRun.latest_per_workflow([first_id], k=2)
        assert [run.github_run_id for run in top_two] == [503, 502]

        assert await AsyncWorkflowRun.latest_per_workflow([], k=1) == []

    async def test_latest_per_workflow_across_workflows(self, bound) -> None:
        """One ORDER BY/LIMIT subquery per workflow, combined with UNION ALL."""
        repo = await _make_repo()
        first_id, second_id = await _seed_two_workflows(repo)

        latest = await AsyncWorkflowRun.latest_per_workflow([first_id, second_id], k=1)

        # Single-query approach: results are in run_number DESC order (not
        # grouped by workflow), so sort by workflow_id for a stable assertion.
        latest.sort(key=lambda r: r.workflow_id)
        assert [run.github_run_id for run in latest] == [503, 510]

    async def test_status_summary_aggregates_per_workflow(self, bound) -> None:
        repo = await _make_repo()
        first_id, second_id = await _seed_two_workflows(repo)

        summary = await AsyncWorkflowRun.status_summary([first_id, second_id])
        summary.sort(key=lambda row: row["workflow_id"])

        assert summary == [
            {
                "workflow_id": first_id,
                "total": 3,
                "completed": 1,
                "success": 1,
                "failure": 0,
                "in_progress": 1,
            },
            {
                "workflow_id": second_id,
                "total": 1,
                "completed": 1,
                "success": 0,
                "failure": 1,
                "in_progress": 0,
            },
        ]

        assert await AsyncWorkflowRun.status_summary([]) == []

    async def test_jobs_returns_stored_jobs_in_id_order(self, bound) -> None:
        repo = await _make_repo()
        run = await AsyncWorkflowRun.adopt_from_remote(
            repo, _run_payload(500, 1, status="completed")
        )
        await AsyncJob.upsert_from_remote(
            run,
            {
                "id": 902,
                "run_id": 500,
                "name": "test",
                "status": "completed",
                "conclusion": "success",
            },
        )
        await AsyncJob.upsert_from_remote(
            run,
            {
                "id": 901,
                "run_id": 500,
                "name": "build",
                "status": "completed",
                "conclusion": "success",
            },
        )

        # A job hangs off an attempt, so "the jobs of this run" is the two-hop
        # read the run exposes. Local id order is what the test asserts.
        jobs = [job for attempt in await run.attempts() for job in await attempt.jobs()]

        assert [job.name for job in jobs] == ["test", "build"]  # local id order


@pytest.mark.parametrize("github_run_id", [500])
def test_run_payload_helper_builds_github_shapes(github_run_id: int) -> None:
    """Guard the fixture helper: ids stay ints, completed runs carry an end."""
    payload = _run_payload(github_run_id, 1, status="completed")
    assert payload["id"] == github_run_id
    assert isinstance(payload["id"], int)
    assert payload["completed_at"] == "2026-10-10T10:05:00Z"
    queued = _run_payload(github_run_id, 1)
    assert "completed_at" not in queued
