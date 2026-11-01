# tests/adapters/test_health_alerts.py
"""A delivery we failed to store is an alert, not a number in a histogram.

``workflow_job`` deliveries arrive one per job, so several of them describe the
same run at the same moment. When storing one of those failed, the ledger
recorded ``IntegrityError`` next to ``ignored`` -- and ``ignored`` means "we
chose not to store a foreign repository". Reading the histogram, a caller cannot
tell a bug from a configuration choice, and the run it described is missing
from the database either way.

So the two are separated: ``ignored`` stays a reason, a write failure becomes a
top-level alert that carries the delivery id and the run/job it described.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rhosocial.github_ci_tracker.adapters.mcp.tools.status import _health_snapshot
from rhosocial.github_ci_tracker.container import Container
from rhosocial.github_ci_tracker.models import AsyncWebhookDelivery


async def _delivery(
    event_type: str = "workflow_job",
    *,
    reason: str = "ignored",
    delivery_id: str = "d-1",
    run_id: int | None = 42,
    job_id: int | None = 7,
    age_minutes: int = 1,
) -> None:
    await AsyncWebhookDelivery(
        delivery_id=delivery_id,
        event_type=event_type,
        received_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
        repository_full_name="acme/demo",
        action="completed",
        github_run_id=run_id,
        github_job_id=job_id,
        handled=False,
        reason=reason,
        is_redelivery=False,
    ).save()


class TestHealthAlerts:
    async def test_a_write_failure_is_raised_out_of_the_histogram(self, bound) -> None:
        await _delivery(reason="IntegrityError: duplicate key value", delivery_id="d-bad")
        await _delivery(reason="ignored", delivery_id="d-ok")

        snapshot = await _health_snapshot(Container.build())

        stream = snapshot["streams"]["workflow_job"]
        assert stream["unhandled_reasons"] == {"IntegrityError": 1, "ignored": 1}
        assert stream["write_failures"] == 1
        assert stream["last_write_failure"]["delivery_id"] == "d-bad"
        assert stream["last_write_failure"]["run_id"] == 42
        assert stream["last_write_failure"]["job_id"] == 7

        assert [alert["code"] for alert in snapshot["alerts"]] == ["webhook_write_failed"]
        # The alert must say how to repair, not only that something broke.
        assert "find_runs" in snapshot["alerts"][0]["hint"]
        assert "get_run" in snapshot["alerts"][0]["hint"]

    async def test_the_alert_lists_the_runs_it_affects(self, bound) -> None:
        """A count says something is missing; the ids say what to re-read.

        One matrix failure is one delivery per job, so the same run arrives many
        times. Listing it once is what makes the repair a single pass.
        """
        await _delivery(reason="DatabaseError: x", delivery_id="d-1", run_id=42, job_id=7)
        await _delivery(reason="DatabaseError: x", delivery_id="d-2", run_id=42, job_id=8)
        await _delivery(reason="DatabaseError: x", delivery_id="d-3", run_id=43, job_id=9)

        snapshot = await _health_snapshot(Container.build())

        assert snapshot["alerts"][0]["runs"] == [43, 42], "newest first, one entry per run"
        assert snapshot["streams"]["workflow_job"]["affected_runs"] == [43, 42]

    async def test_expected_unhandled_reasons_are_not_alerts(self, bound) -> None:
        await _delivery(reason="ignored")
        await _delivery(reason="unsupported_event")
        await _delivery(reason="invalid_json")
        await _delivery(reason="signature_failed")

        snapshot = await _health_snapshot(Container.build())

        assert snapshot["alerts"] == []
        assert snapshot["streams"]["workflow_job"]["write_failures"] == 0

    async def test_the_streams_stay_separate(self, bound) -> None:
        await _delivery(event_type="workflow_run", reason="ignored", delivery_id="d-run")
        await _delivery(event_type="workflow_job", reason="IntegrityError: x", delivery_id="d-job")

        snapshot = await _health_snapshot(Container.build())

        assert snapshot["streams"]["workflow_run"]["write_failures"] == 0
        assert [alert["stream"] for alert in snapshot["alerts"]] == ["workflow_job"]
