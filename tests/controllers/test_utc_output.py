# tests/controllers/test_utc_output.py
"""Every timestamp leaves the service in UTC, whatever timezone it came in with.

GitHub payloads carry an offset; Postgres hands rows back in the connection's
timezone. Rendering both as-is put ``+08:00`` and ``+00:00`` in one response, so
the view layer normalizes once, here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from rhosocial.github_ci_tracker.controllers import RepositoryData, RunData
from rhosocial.github_ci_tracker.controllers.status import StatusController
from rhosocial.github_ci_tracker.controllers.webhook import DeliveryLedger
from rhosocial.github_ci_tracker.models import AsyncWebhookDelivery
from rhosocial.github_ci_tracker.views import (
    _to_job_record,
    _to_run_record,
    _to_step_record,
    as_utc,
)

SHANGHAI = timezone(timedelta(hours=8))
UTC = timezone.utc


def test_a_plus_eight_value_becomes_the_same_instant_in_utc() -> None:
    local = datetime(2026, 10, 10, 18, 0, tzinfo=SHANGHAI)

    converted = as_utc(local)

    assert converted is not None
    assert converted.tzinfo is not None, "the value stays timezone-aware"
    assert converted.utcoffset() == timedelta(0)
    assert converted.hour == 10
    assert converted == local, "the instant must not move"


def test_a_naive_value_is_taken_as_utc() -> None:
    converted = as_utc(datetime(2026, 10, 10, 10, 0))

    assert converted is not None
    assert converted.tzinfo is not None, "a guessed zone is still a stated zone"
    assert converted.utcoffset() == timedelta(0)
    assert converted.hour == 10


def test_none_stays_none() -> None:
    assert as_utc(None) is None


@dataclass
class _Run:
    github_run_id: int = 500
    run_number: int = 1
    latest_attempt: int = 1
    latest_status: str = "completed"
    latest_conclusion: str | None = "success"
    branch: str = "main"
    commit_sha: str = "abc"
    commit_message: str = "msg"
    actor: str = "alice"
    event: str = "push"
    pr_number: int | None = None
    created_at: datetime | None = None
    latest_github_created_at: datetime | None = None
    latest_started_at: datetime | None = None
    latest_completed_at: datetime | None = None


class TestRecordsAreUTC:
    def test_a_run_record_normalizes_every_timestamp(self) -> None:
        run = _Run(
            created_at=datetime(2026, 10, 10, 18, 7, tzinfo=SHANGHAI),
            latest_github_created_at=datetime(2026, 10, 10, 18, 0, tzinfo=SHANGHAI),
            latest_started_at=datetime(2026, 10, 10, 18, 1, tzinfo=SHANGHAI),
            latest_completed_at=datetime(2026, 10, 10, 18, 6, tzinfo=SHANGHAI),
        )

        record = _to_run_record(run, "acme/demo")  # type: ignore[arg-type]

        for value in (record.created_at, record.started_at, record.completed_at):
            assert value is not None
            assert value.tzinfo is not None
            assert value.utcoffset() == timedelta(0)
        assert record.started_at.hour == 10
        assert record.duration_seconds == 300
        # ``created_at`` is GitHub's, ``stored_at`` is this process's own write.
        assert record.created_at.hour == 10
        assert record.stored_at is not None and record.stored_at.hour == 10
        assert record.created_at != record.stored_at

    def test_a_job_record_normalizes_its_timestamps(self) -> None:
        job = SimpleNamespace(
            github_job_id=900,
            name="build",
            status="completed",
            conclusion="success",
            runner_name="ubuntu-latest",
            started_at=datetime(2026, 10, 10, 18, 0, tzinfo=SHANGHAI),
            completed_at=datetime(2026, 10, 10, 18, 5, tzinfo=SHANGHAI),
        )

        record = _to_job_record(job)  # type: ignore[arg-type]

        assert record.started_at is not None
        assert record.started_at.tzinfo is not None
        assert record.started_at.utcoffset() == timedelta(0)
        assert record.completed_at is not None
        assert record.completed_at.hour == 10

    def test_a_step_record_normalizes_its_timestamps(self) -> None:
        step = SimpleNamespace(
            github_step_id=1,
            name="checkout",
            status="completed",
            conclusion="success",
            number=1,
            started_at=datetime(2026, 10, 10, 18, 0, tzinfo=SHANGHAI),
            completed_at=datetime(2026, 10, 10, 18, 1, tzinfo=SHANGHAI),
        )

        record = _to_step_record(step)  # type: ignore[arg-type]

        assert record.started_at is not None
        assert record.started_at.tzinfo is not None
        assert record.started_at.utcoffset() == timedelta(0)


class TestTokenRecordIsUTC:
    def test_credential_timestamps_are_aware_and_utc(self) -> None:
        """The token routes serialize these fields too."""
        from types import SimpleNamespace

        from rhosocial.github_ci_tracker.controllers.token import TokenController

        record = SimpleNamespace(
            id=1,
            name="work",
            scope="repo,workflow",
            owner_login="alice",
            is_active=True,
            expires_at=datetime(2026, 11, 1, 18, 0, tzinfo=SHANGHAI),
            can_read_actions=True,
            can_write_actions=None,
            can_manage_webhooks=None,
            created_at=datetime(2026, 10, 10, 18, 0, tzinfo=SHANGHAI),
            updated_at=datetime(2026, 10, 11, 18, 0, tzinfo=SHANGHAI),
        )

        view = TokenController._view(record)  # noqa: SLF001 - the conversion under test

        for value in (view.expires_at, view.created_at, view.updated_at):
            assert value is not None
            assert value.tzinfo is not None
            assert value.utcoffset() == timedelta(0)
        assert view.created_at.hour == 10


class TestStatusIsUTC:
    async def test_the_stream_and_failure_timestamps_are_utc(self, bound) -> None:
        await AsyncWebhookDelivery(
            delivery_id="d-1",
            event_type="workflow_run",
            received_at=datetime.now(SHANGHAI),
            repository_full_name="acme/demo",
            handled=False,
            reason="IntegrityError: duplicate",
            is_redelivery=False,
        ).save()

        snapshot = await StatusController(
            repositories=RepositoryData(),
            runs=RunData(),
            deliveries=DeliveryLedger(),
        ).snapshot()

        last_at = snapshot.streams["workflow_run"].last_at
        assert last_at is not None
        assert last_at.utcoffset() == timedelta(0)

        alert = snapshot.alerts[0]
        received_at = alert["last"]["received_at"]
        assert received_at.utcoffset() == timedelta(0)
