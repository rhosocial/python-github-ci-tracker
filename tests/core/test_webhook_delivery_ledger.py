# tests/core/test_webhook_delivery_ledger.py
"""The delivery ledger answers "what arrived, and what did we do with it".

Three properties are pinned here, because each one is a mistake that is easy to
make and expensive to notice:

* the two GitHub streams stay separate -- a ``workflow_job`` delivery never
  overwrites the run-level signal, and each stream reports its own counts;
* a redelivery is recorded as a second row, not silently flattened into the
  first: pressing "Redeliver" and GitHub sending on its own differ only in when
  the delivery was sent, and the ledger keeps both facts;
* a rejected delivery (bad signature) is still recorded, so an operator can tell
  "GitHub never reached us" from "it arrived and we refused it".
"""

from __future__ import annotations

from conftest import make_webhook_service

from rhosocial.github_ci_tracker.models import (
    AsyncRepository,
    AsyncWebhookDelivery,
    AsyncWorkflow,
    AsyncWorkflowRun,
)
from rhosocial.github_ci_tracker.models.webhook_delivery import (
    EVENT_WORKFLOW_JOB,
    EVENT_WORKFLOW_RUN,
    REASON_IGNORED,
    REASON_PROCESSED,
    REASON_SIGNATURE_FAILED,
)

REPO = "acme/demo"
RUN_ID = 4242


async def _tracked_run() -> AsyncWorkflowRun:
    repo = AsyncRepository(github_repo_id=99, full_name=REPO, owner="acme", name="demo")
    await repo.save()
    workflow = AsyncWorkflow(
        repository_id=repo.id or 0,
        workflow_id=7,
        name="CI",
        path=".github/workflows/test.yml",
    )
    await workflow.save()
    run = AsyncWorkflowRun(
        workflow_id=workflow.id or 0,
        github_run_id=RUN_ID,
        run_number=1,
        run_attempt=1,
        status="in_progress",
        conclusion=None,
        branch="main",
        commit_sha="0" * 40,
        event="workflow_dispatch",
    )
    await run.save()
    return run


def _job_payload() -> dict:
    return {
        "action": "in_progress",
        "repository": {"id": 99, "full_name": REPO},
        "workflow_job": {
            "id": 5001,
            "run_id": RUN_ID,
            "name": "tests",
            "status": "in_progress",
            "conclusion": None,
            "steps": [],
        },
    }


def _run_payload(action: str = "completed") -> dict:
    return {
        "action": action,
        "repository": {"id": 99, "full_name": REPO},
        "workflow_run": {
            "id": RUN_ID,
            "run_number": 1,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
            "head_branch": "main",
            "head_sha": "0" * 40,
            "event": "workflow_dispatch",
        },
    }


async def _deliveries() -> list[AsyncWebhookDelivery]:
    return await AsyncWebhookDelivery.query().order_by((AsyncWebhookDelivery.c.id, "ASC")).all()


class TestRecording:
    async def test_a_job_delivery_is_recorded_with_both_ids(self, bound) -> None:
        await _tracked_run()
        service = make_webhook_service()

        result = await service.process_webhook("workflow_job", _job_payload(), delivery_id="d-1")
        assert result["handled"] is True
        assert result["reason"] == REASON_PROCESSED

        rows = await _deliveries()
        assert len(rows) == 1
        row = rows[0]
        assert row.event_type == EVENT_WORKFLOW_JOB
        assert row.delivery_id == "d-1"
        assert row.github_run_id == RUN_ID, "a job delivery still names its run"
        assert row.github_job_id == 5001
        assert row.repository_full_name == REPO
        assert row.is_redelivery is False

    async def test_the_two_streams_do_not_overwrite_each_other(self, bound) -> None:
        """A job delivery must not touch the run-level timestamp."""
        run = await _tracked_run()
        service = make_webhook_service()

        await service.process_webhook("workflow_job", _job_payload(), delivery_id="d-1")
        after_job = await AsyncWorkflowRun.find_by_github_id(RUN_ID)
        assert after_job is not None
        assert after_job.webhook_received_at is None, (
            "only a workflow_run delivery stamps the run; the ledger carries the "
            "job stream instead of polluting the run's own signal"
        )

        await service.process_webhook("workflow_run", _run_payload(), delivery_id="d-2")
        after_run = await AsyncWorkflowRun.find_by_github_id(RUN_ID)
        assert after_run is not None
        assert after_run.webhook_received_at is not None

        rows = await _deliveries()
        assert [row.event_type for row in rows] == [EVENT_WORKFLOW_JOB, EVENT_WORKFLOW_RUN]
        assert run.webhook_received_at is None

    async def test_a_foreign_run_is_recorded_as_ignored(self, bound) -> None:
        """The delivery still happened; only the state change did not."""
        payload = _job_payload()
        payload["workflow_job"]["run_id"] = 999999
        service = make_webhook_service()

        result = await service.process_webhook("workflow_job", payload, delivery_id="d-1")
        assert result["handled"] is False
        assert result["reason"] == REASON_IGNORED

        rows = await _deliveries()
        assert len(rows) == 1
        assert rows[0].handled is False
        assert rows[0].reason == REASON_IGNORED

    async def test_a_rejected_signature_is_recorded(self, bound) -> None:
        """Requested by the receiver when the HMAC check fails, before dispatch."""
        service = make_webhook_service()

        await service.record_delivery(
            event_type="workflow_run",
            delivery_id="d-bad",
            handled=False,
            reason=REASON_SIGNATURE_FAILED,
        )

        rows = await _deliveries()
        assert len(rows) == 1
        assert rows[0].reason == REASON_SIGNATURE_FAILED
        assert rows[0].handled is False
        assert rows[0].github_run_id is None


class TestRedelivery:
    async def test_the_same_delivery_id_twice_is_two_rows(self, bound) -> None:
        await _tracked_run()
        service = make_webhook_service()

        await service.process_webhook("workflow_job", _job_payload(), delivery_id="same-id")
        await service.process_webhook("workflow_job", _job_payload(), delivery_id="same-id")

        rows = await _deliveries()
        assert len(rows) == 2, "a redelivery is an arrival in its own right"
        assert rows[0].is_redelivery is False
        assert rows[1].is_redelivery is True

    async def test_different_delivery_ids_are_both_original(self, bound) -> None:
        await _tracked_run()
        service = make_webhook_service()

        await service.process_webhook("workflow_job", _job_payload(), delivery_id="a")
        await service.process_webhook("workflow_job", _job_payload(), delivery_id="b")

        rows = await _deliveries()
        assert [row.is_redelivery for row in rows] == [False, False]


class TestHealthStreams:
    async def test_streams_are_reported_separately(self, bound) -> None:
        from rhosocial.github_ci_tracker.adapters.mcp.tools.status import _health_snapshot
        from rhosocial.github_ci_tracker.container import Container

        await _tracked_run()
        service = make_webhook_service()
        await service.process_webhook("workflow_job", _job_payload(), delivery_id="job-1")
        await service.process_webhook("workflow_job", _job_payload(), delivery_id="job-1")
        await service.process_webhook("workflow_run", _run_payload(), delivery_id="run-1")

        health = await _health_snapshot(Container.build())
        assert health["ledger_available"] is True

        job = health["streams"][EVENT_WORKFLOW_JOB]
        run = health["streams"][EVENT_WORKFLOW_RUN]
        assert job["deliveries_total"] == 2
        assert job["redelivered"] == 1
        assert run["deliveries_total"] == 1
        assert run["redelivered"] == 0

        # The legacy fields keep working, now fed by the ledger.
        assert health["last_webhook_at"] == run["last_at"]
        assert health["webhooks_in_last_hour"] == 3
        assert health["push_channel"] == "active"
