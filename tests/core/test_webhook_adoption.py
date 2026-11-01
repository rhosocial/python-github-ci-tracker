# tests/core/test_webhook_adoption.py
"""A run of a subscribed repository is tracked whatever started it.

The push channel exists so that an agent can commit and push without telling
the tracker anything: GitHub delivers the ``workflow_run`` event and the run
appears. That only works if the handler adopts runs it did not trigger itself,
so the subscription -- not the trigger -- is what authorizes tracking.

The complementary rule matters just as much: an unsubscribed repository is
still refused, and only appears in the delivery ledger as unhandled.
"""

from __future__ import annotations

from conftest import make_webhook_service

from rhosocial.github_ci_tracker.models import AsyncRepository, AsyncWorkflowRun

REPO = "acme/demo"
RUN_ID = 7777


async def _subscribe(full_name: str = REPO) -> AsyncRepository:
    owner, _, name = full_name.partition("/")
    repo = AsyncRepository(
        github_repo_id=abs(hash(full_name)) % 1_000_000,
        full_name=full_name,
        owner=owner,
        name=name,
    )
    await repo.save()
    return repo


def _run_payload(*, repo: str = REPO, run_id: int = RUN_ID, action: str = "completed") -> dict:
    completed = action == "completed"
    return {
        "action": action,
        "repository": {"id": 1, "full_name": repo},
        "workflow_run": {
            "id": run_id,
            "run_number": 12,
            "run_attempt": 1,
            "status": "completed" if completed else "in_progress",
            "conclusion": "success" if completed else None,
            "head_branch": "feature/x",
            "head_sha": "a" * 40,
            "event": "push",
            "head_commit": {"message": "fix things"},
            "actor": {"login": "someone"},
            # Real payloads always identify the workflow; without these the
            # handler cannot deduplicate the second delivery of the same run.
            "workflow_id": 7,
            "path": ".github/workflows/test.yml",
            "name": "CI",
        },
    }


class TestAdoption:
    async def test_a_push_triggered_run_is_adopted(self, bound) -> None:
        """Nobody told the tracker about this run; the subscription did."""
        await _subscribe()
        handler = make_webhook_service()

        handled = await handler.handle_event("workflow_run", _run_payload())

        assert handled is True
        run = await AsyncWorkflowRun.find_by_github_id(RUN_ID, derived=True)
        assert run is not None, "a pushed commit's run must be tracked without a trigger"
        assert run.branch == "feature/x"
        assert run.event == "push"
        assert run.latest_conclusion == "success"
        assert run.webhook_received_at is not None

    async def test_a_pr_triggered_run_is_adopted(self, bound) -> None:
        payload = _run_payload()
        payload["workflow_run"]["event"] = "pull_request"
        payload["workflow_run"]["pull_requests"] = [{"number": 42}]
        await _subscribe()
        handler = make_webhook_service()

        assert await handler.handle_event("workflow_run", payload) is True
        run = await AsyncWorkflowRun.find_by_github_id(RUN_ID)
        assert run is not None
        assert run.pr_number == 42

    async def test_an_unsubscribed_repository_is_still_refused(self, bound) -> None:
        handler = make_webhook_service()

        handled = await handler.handle_event("workflow_run", _run_payload(repo="acme/not-tracked"))

        assert handled is False
        assert await AsyncWorkflowRun.find_by_github_id(RUN_ID) is None

    async def test_a_repeated_delivery_updates_instead_of_duplicating(self, bound) -> None:
        await _subscribe()
        handler = make_webhook_service()

        assert (
            await handler.handle_event("workflow_run", _run_payload(action="in_progress")) is True
        )
        first = await AsyncWorkflowRun.find_by_github_id(RUN_ID, derived=True)
        assert first is not None and first.latest_status == "in_progress"

        assert await handler.handle_event("workflow_run", _run_payload(action="completed")) is True
        second = await AsyncWorkflowRun.find_by_github_id(RUN_ID, derived=True)
        assert second is not None and second.latest_conclusion == "success"
        assert await AsyncWorkflowRun.query().count() == 1
