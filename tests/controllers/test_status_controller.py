# tests/controllers/test_status_controller.py
"""``StatusController``: the health snapshot, assembled from rows and the ledger.

The figures are two reads that must not be confused: the stored rows answer "what
do we track", the delivery ledger answers "is GitHub reaching us". A missing
ledger costs only the second, and a delivery we failed to store is reported as
an alert rather than as another reason in the histogram.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rhosocial.github_ci_tracker.controllers import RepositoryData, RunData
from rhosocial.github_ci_tracker.controllers.status import StatusController
from rhosocial.github_ci_tracker.controllers.webhook import DeliveryLedger
from rhosocial.github_ci_tracker.models import AsyncRepository, AsyncWebhookDelivery


def _service() -> StatusController:
    return StatusController(
        repositories=RepositoryData(),
        runs=RunData(),
        deliveries=DeliveryLedger(),
    )


async def _delivery(
    event_type: str = "workflow_run",
    *,
    reason: str = "processed",
    delivery_id: str = "d-1",
    handled: bool = True,
    age_minutes: int = 1,
    redelivery: bool = False,
) -> None:
    await AsyncWebhookDelivery(
        delivery_id=delivery_id,
        event_type=event_type,
        received_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
        repository_full_name="acme/demo",
        action="completed",
        github_run_id=42,
        handled=handled,
        reason=reason,
        is_redelivery=redelivery,
    ).save()


async def _repository(full_name: str = "acme/demo") -> AsyncRepository:
    owner, name = full_name.split("/", 1)
    repo = AsyncRepository(
        github_repo_id=abs(hash(full_name)) % 100_000,
        full_name=full_name,
        owner=owner,
        name=name,
    )
    await repo.save()
    return repo


class TestSnapshotCounts:
    async def test_the_counts_come_from_the_stored_rows(self, bound) -> None:
        await _repository()
        await _repository("acme/other")
        await _delivery()

        snapshot = await _service().snapshot()

        assert snapshot.counts_available is True
        assert snapshot.repository_count == 2
        assert snapshot.runs_tracked == 0

    async def test_a_missing_ledger_costs_only_the_webhook_figures(self, bound) -> None:
        """An old database has no ledger; the counts are still worth reporting."""

        class _BrokenLedger:
            async def latest_received(self, event_type):  # noqa: ANN001, ANN202
                raise RuntimeError("no such table")

            async def count(
                self, event_type, *, since=None, redelivered=None
            ):  # noqa: ANN001, ANN202
                raise RuntimeError("no such table")

            async def unhandled_since(self, event_type, since):  # noqa: ANN001, ANN202
                raise RuntimeError("no such table")

        await _repository()
        service = StatusController(
            repositories=RepositoryData(),
            runs=RunData(),
            deliveries=_BrokenLedger(),  # type: ignore[arg-type]
        )

        snapshot = await service.snapshot()

        assert snapshot.ledger_available is False
        assert snapshot.counts_available is True
        assert snapshot.repository_count == 1
        assert "no such table" in (snapshot.error or "")
        assert snapshot.streams == {}


class TestIngestRate:
    """A backfill and "the data is gone" look identical in a total count."""

    async def test_rows_added_just_now_are_counted_in_both_windows(self, bound) -> None:
        repo = await _repository()
        from rhosocial.github_ci_tracker.models import AsyncWorkflow, AsyncWorkflowRun

        workflow = AsyncWorkflow(
            repository_id=repo.id or 0,
            workflow_id=7,
            name="CI",
            path=".github/workflows/ci.yml",
        )
        await workflow.save()
        for run_id in (1, 2, 3):
            await AsyncWorkflowRun(
                workflow_id=workflow.id or 0,
                github_run_id=run_id,
                run_number=run_id,
                run_attempt=1,
                status="completed",
            ).save()

        snapshot = await _service().snapshot()

        assert snapshot.ingest["available"] is True
        assert snapshot.ingest["runs_added_last_5m"] == 3
        assert snapshot.ingest["runs_added_last_hour"] >= 3
        assert snapshot.ingest["backfill_suspected"] is False

    async def test_a_burst_is_flagged_as_a_backfill(self, bound) -> None:
        """The caller must be able to tell "still loading" from "lost"."""
        repo = await _repository()
        from rhosocial.github_ci_tracker.models import AsyncWorkflow, AsyncWorkflowRun

        workflow = AsyncWorkflow(
            repository_id=repo.id or 0,
            workflow_id=7,
            name="CI",
            path=".github/workflows/ci.yml",
        )
        await workflow.save()
        for run_id in range(40):
            await AsyncWorkflowRun(
                workflow_id=workflow.id or 0,
                github_run_id=1_000 + run_id,
                run_number=run_id,
                run_attempt=1,
                status="completed",
            ).save()

        snapshot = await _service().snapshot()

        assert snapshot.ingest["backfill_suspected"] is True

    async def test_an_unreadable_table_does_not_fail_the_snapshot(self, bound) -> None:
        class _BrokenRuns(RunData):
            async def added_since(self, cutoff):  # noqa: ANN001, ANN202
                raise RuntimeError("no such table")

        service = StatusController(
            repositories=RepositoryData(),
            runs=_BrokenRuns(),
            deliveries=DeliveryLedger(),
        )

        snapshot = await service.snapshot()

        assert snapshot.ingest["available"] is False
        assert "no such table" in snapshot.ingest["error"]


class TestPushChannel:
    async def test_a_recent_delivery_makes_the_channel_active(self, bound) -> None:
        await _delivery(age_minutes=5)

        snapshot = await _service().snapshot()

        assert snapshot.push_channel == "active"
        assert snapshot.webhooks_in_last_hour == 1

    async def test_an_old_delivery_makes_it_stale(self, bound) -> None:
        await _delivery(age_minutes=120)

        snapshot = await _service().snapshot()

        assert snapshot.push_channel == "stale"
        assert snapshot.webhooks_in_last_hour == 0
        assert snapshot.streams["workflow_run"].deliveries_total == 1

    async def test_no_delivery_at_all_is_unknown(self, bound) -> None:
        snapshot = await _service().snapshot()

        assert snapshot.push_channel == "unknown"
        assert snapshot.last_webhook_at is None

    async def test_a_redelivery_is_counted_as_a_delivery(self, bound) -> None:
        await _delivery(delivery_id="d-1", redelivery=False)
        await _delivery(delivery_id="d-1", redelivery=True, age_minutes=2)

        health = (await _service().snapshot()).streams["workflow_run"]

        assert health.deliveries_total == 2
        assert health.redelivered == 1


class TestAlerts:
    async def test_a_failed_write_is_an_alert_not_a_reason(self, bound) -> None:
        await _delivery(
            event_type="workflow_job",
            reason="IntegrityError: duplicate key",
            delivery_id="d-bad",
            handled=False,
        )
        await _delivery(
            event_type="workflow_job", reason="ignored", handled=False, delivery_id="d-ok"
        )

        snapshot = await _service().snapshot()

        assert snapshot.alerts == [
            {
                "code": "webhook_write_failed",
                "stream": "workflow_job",
                "count": 1,
                "last": snapshot.alerts[0]["last"],
            }
        ]
        last = snapshot.alerts[0]["last"]
        assert last["delivery_id"] == "d-bad"
        assert last["reason"] == "IntegrityError: duplicate key"

    async def test_the_expected_reasons_are_not_alerts(self, bound) -> None:
        for reason in ("ignored", "unsupported_event", "invalid_json", "signature_failed"):
            await _delivery(reason=reason, handled=False, delivery_id=f"d-{reason}")

        snapshot = await _service().snapshot()

        assert snapshot.alerts == []
        assert snapshot.streams["workflow_run"].write_failures == 0
        assert snapshot.streams["workflow_run"].unhandled == 4
