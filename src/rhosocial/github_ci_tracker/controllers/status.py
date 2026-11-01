# src/rhosocial/github_ci_tracker/controllers/status.py
"""The health snapshot: what this service tracks, and whether GitHub reaches it.

Two sources, and the split is the point:

* the stored rows answer "what do we track" -- how many repositories, how many
  runs;
* the delivery ledger answers "is the push channel alive" -- every delivery that
  arrived, accepted, ignored or rejected.

A redelivered event counts like an original one: the difference is when it was
sent, not whether the channel works. What separates "working as designed" from
"our own write failed" is the reason code, which is why the reasons are grouped
and the unexpected ones are promoted to an alert: a delivery the service could
not store means the run or job it described is missing from the database, and
nothing else would notice.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..views import (
    PUSH_CHANNEL_ACTIVE,
    PUSH_CHANNEL_STALE,
    PUSH_CHANNEL_UNKNOWN,
    StatusSnapshot,
    StreamHealth,
    as_utc,
)
from .webhook import (
    EVENT_WORKFLOW_JOB,
    EVENT_WORKFLOW_RUN,
    REASON_IGNORED,
    REASON_INVALID_JSON,
    REASON_SIGNATURE_FAILED,
    REASON_UNSUPPORTED_EVENT,
)

if TYPE_CHECKING:  # the rows these flows return; only the annotation names them
    from ..controllers import RepositoryData, RunData
    from ..models import AsyncWebhookDelivery
    from .webhook import DeliveryLedger

logger = logging.getLogger(__name__)

#: How far back "recent" reaches for the live/stale verdict, and for the
#: unhandled histogram.
RECENT_WINDOW = timedelta(hours=1)
UNHANDLED_WINDOW = timedelta(hours=24)
#: How many affected run ids an alert lists. Enough to cover a bad deploy, few
#: enough that the payload stays readable; the rest are counted, not listed.
AFFECTED_RUNS_REPORTED = 10

#: Reasons that mean "we chose not to store this delivery". Anything else on an
#: unhandled row is our own failure, so it is reported as an alert.
EXPECTED_UNHANDLED = frozenset(
    {REASON_IGNORED, REASON_UNSUPPORTED_EVENT, REASON_INVALID_JSON, REASON_SIGNATURE_FAILED}
)

#: The alert code for a delivery we received and failed to store.
ALERT_WRITE_FAILED = "webhook_write_failed"

#: The two windows the ingest counters report.
INGEST_SHORT_WINDOW = timedelta(minutes=5)
INGEST_LONG_WINDOW = timedelta(hours=1)

#: Run rows added in five minutes above this is a burst: a webhook trickle is
#: one insert per finished job, while a repair or a backfill walks history.
INGEST_BACKFILL_BURST = 25


class StatusController:
    """Assemble the snapshot the status surface reports.

    Args:
        repositories: Repository rows and their credential bindings.
        runs: Stored run rows.
        deliveries: The webhook delivery ledger.
    """

    def __init__(
        self,
        *,
        repositories: RepositoryData,
        runs: RunData,
        deliveries: DeliveryLedger,
    ) -> None:
        self._repositories = repositories
        self._runs = runs
        self._deliveries = deliveries

    async def snapshot(self) -> StatusSnapshot:
        """Read the counts and the per-stream ledger figures.

        Never raises: a health report that fails is worse than one that says it
        could not measure something. The two failure modes are reported apart --
        an unreadable database at all, and an unreadable ledger -- because the
        second is a stale schema and only costs the webhook figures.
        """
        try:
            repository_count = await self._repositories.count()
            runs_tracked = await self._runs.count()
        except Exception as exc:  # noqa: BLE001 - health must never fail the tool
            return StatusSnapshot(
                counts_available=False,
                ledger_available=False,
                error=f"{exc.__class__.__name__}: {exc}",
            )

        now = datetime.now(timezone.utc)
        recent_cutoff = now - RECENT_WINDOW
        unhandled_since = now - UNHANDLED_WINDOW

        try:
            streams = {
                event_type: await self._stream(event_type, recent_cutoff, unhandled_since)
                for event_type in (EVENT_WORKFLOW_RUN, EVENT_WORKFLOW_JOB)
            }
        except Exception as exc:  # noqa: BLE001 - an old database has no ledger yet
            return StatusSnapshot(
                repository_count=repository_count,
                runs_tracked=runs_tracked,
                ledger_available=False,
                error=f"{exc.__class__.__name__}: {exc}",
            )

        newest = max(
            (health.last_at for health in streams.values() if health.last_at),
            default=None,
        )
        return StatusSnapshot(
            repository_count=repository_count,
            runs_tracked=runs_tracked,
            streams=streams,
            ingest=await self._ingest(now),
            alerts=[
                {
                    "code": ALERT_WRITE_FAILED,
                    "stream": event_type,
                    "count": health.write_failures,
                    "last": health.last_write_failure,
                }
                for event_type, health in streams.items()
                if health.write_failures
            ],
            last_webhook_at=streams[EVENT_WORKFLOW_RUN].last_at,
            webhooks_in_last_hour=sum(health.in_last_hour for health in streams.values()),
            push_channel=(
                PUSH_CHANNEL_UNKNOWN
                if newest is None
                else (PUSH_CHANNEL_ACTIVE if newest >= recent_cutoff else PUSH_CHANNEL_STALE)
            ),
        )

    # -- internals -------------------------------------------------------------

    async def _ingest(self, now: datetime) -> dict[str, Any]:
        """How fast run rows are arriving, so a backfill is visible as one.

        ``runs_tracked`` alone cannot answer "is this still loading or did we
        lose data": the same total looks the same either way, and a repair pass
        that walks history changes it under the caller's feet. The two windows
        make the difference legible -- a burst of inserts with no matching
        webhook traffic is a backfill, and it ends.

        A failure here is reported in the block rather than raised: the two
        counters are the part of the snapshot that is least worth failing over.
        """
        try:
            last_5m = await self._runs.added_since(now - INGEST_SHORT_WINDOW)
            last_hour = await self._runs.added_since(now - INGEST_LONG_WINDOW)
        except Exception as exc:  # noqa: BLE001 - a snapshot reports, never raises
            return {"available": False, "error": f"{exc.__class__.__name__}: {exc}"}
        return {
            "available": True,
            "runs_added_last_5m": last_5m,
            "runs_added_last_hour": last_hour,
            "backfill_suspected": last_5m >= INGEST_BACKFILL_BURST,
        }

    async def _stream(
        self,
        event_type: str,
        recent_cutoff: datetime,
        unhandled_since: datetime,
    ) -> StreamHealth:
        """One event stream's figures, and the failures worth alerting on."""
        unhandled_rows = await self._deliveries.unhandled_since(event_type, unhandled_since)

        reasons: dict[str, int] = {}
        failures: list[AsyncWebhookDelivery] = []
        for row in unhandled_rows:
            key = (row.reason or "unknown").split(":")[0][:48]
            reasons[key] = reasons.get(key, 0) + 1
            if key not in EXPECTED_UNHANDLED:
                failures.append(row)

        last_failure = max(
            failures,
            key=lambda row: row.received_at or unhandled_since,
            default=None,
        )
        # Newest first, one entry per run: a matrix failure is one delivery per
        # job and would otherwise fill the list with the same run.
        affected: list[int] = []
        for row in sorted(
            failures,
            key=lambda item: item.received_at or unhandled_since,
            reverse=True,
        ):
            run_id = row.github_run_id
            if run_id and run_id not in affected:
                affected.append(run_id)
            if len(affected) >= AFFECTED_RUNS_REPORTED:
                break

        return StreamHealth(
            event_type=event_type,
            last_at=as_utc(await self._deliveries.latest_received(event_type)),
            in_last_hour=await self._deliveries.count(event_type, since=recent_cutoff),
            deliveries_total=await self._deliveries.count(event_type),
            redelivered=await self._deliveries.count(event_type, redelivered=True),
            unhandled=len(unhandled_rows),
            unhandled_reasons=reasons,
            write_failures=len(failures),
            last_write_failure=self._write_failure(last_failure),
            affected_runs=affected,
        )

    @staticmethod
    def _write_failure(row: Any) -> dict[str, Any] | None:
        """One failing row, reduced to what an operator needs to go and look."""
        if row is None:
            return None
        return {
            "delivery_id": row.delivery_id,
            "received_at": as_utc(row.received_at),
            "repository": row.repository_full_name,
            "run_id": row.github_run_id,
            "job_id": row.github_job_id,
            "reason": row.reason,
        }


__all__ = [
    "AFFECTED_RUNS_REPORTED",
    "ALERT_WRITE_FAILED",
    "EXPECTED_UNHANDLED",
    "RECENT_WINDOW",
    "StatusController",
    "UNHANDLED_WINDOW",
]
