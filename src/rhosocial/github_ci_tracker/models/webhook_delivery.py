# src/rhosocial/github_ci_tracker/models/webhook_delivery.py
"""Delivery ledger: one row per GitHub webhook delivery that reached us.

This is transport evidence, not resource state. ``ct_workflow_runs`` /
``ct_jobs`` / ``ct_steps`` record what a run *is*; this table records that a
delivery *arrived*, which stream it belonged to, and what we did with it. The
two are deliberately separate:

* ``ct_workflow_runs.webhook_received_at`` stays "the last ``workflow_run``
  event for this run" and nothing else, so a job delivery can never overwrite
  it;
* the ledger keeps the ``workflow_run`` and ``workflow_job`` streams apart by
  ``event_type`` -- GitHub emits them separately, with separate payloads and
  separate lifecycles -- while sharing one recording path, one cleanup rule and
  one index strategy.

No payload is stored. GitHub keeps deliveries for 30 days and ``delivery_id``
is the key to fetch one back, so copying bodies here would only duplicate what
we can already retrieve, at the cost of a table that grows with every job.

``delivery_id`` is **not** unique on purpose: pressing "Redeliver" in GitHub
re-sends the same id, and that second arrival is a fact worth keeping (it is
what ``is_redelivery`` marks). Uniqueness would erase exactly the distinction
this table exists to make.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from rhosocial.activerecord.base.field_proxy import FieldProxy
from rhosocial.activerecord.field.timestamp import DefaultTimestampMixin
from rhosocial.activerecord.model import ActiveRecord, AsyncActiveRecord
from rhosocial.activerecord.query import ActiveQuery, AsyncActiveQuery

#: GitHub event names this service subscribes to. Kept as constants because they
#: are the partition key of the ledger and appear in API responses.
EVENT_WORKFLOW_RUN = "workflow_run"
EVENT_WORKFLOW_JOB = "workflow_job"

#: Outcome codes. Stable strings: they are part of the API surface.
REASON_PROCESSED = "processed"
REASON_IGNORED = "ignored"
REASON_SIGNATURE_FAILED = "signature_failed"
REASON_INVALID_JSON = "invalid_json"
REASON_UNSUPPORTED_EVENT = "unsupported_event"


class WebhookDelivery(DefaultTimestampMixin, ActiveRecord):
    """One delivery, sync twin."""

    __table_name__ = "ct_webhook_delivery"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    #: ``X-GitHub-Delivery``; repeated on a redelivery, so not unique.
    delivery_id: str = ""
    #: ``X-GitHub-Event``: which stream this delivery belongs to.
    event_type: str = ""
    #: When the receiver accepted the request, not when GitHub sent it.
    received_at: datetime | None = None
    repository_full_name: str = ""
    #: ``action`` of the payload when it carries one (``completed``, ``in_progress``).
    action: str | None = None
    #: GitHub ids, so a delivery can be traced to the run/job/step it described
    #: without storing the body. A job delivery carries both ids.
    github_run_id: int | None = None
    github_job_id: int | None = None
    #: Whether the handler accepted it as a change to tracked state.
    handled: bool = False
    reason: str | None = None
    #: True when this ``delivery_id`` had already been seen.
    is_redelivery: bool = False

    @classmethod
    def recent(cls, limit: int = 50) -> ActiveQuery:
        """Query builder over the newest deliveries."""
        return cls.query().order_by((cls.c.received_at, "DESC")).limit(limit)


class AsyncWebhookDelivery(DefaultTimestampMixin, AsyncActiveRecord):
    """Async twin of :class:`WebhookDelivery`."""

    __table_name__ = "ct_webhook_delivery"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    delivery_id: str = ""
    event_type: str = ""
    received_at: datetime | None = None
    repository_full_name: str = ""
    action: str | None = None
    github_run_id: int | None = None
    github_job_id: int | None = None
    handled: bool = False
    reason: str | None = None
    is_redelivery: bool = False

    @classmethod
    def recent(cls, limit: int = 50) -> AsyncActiveQuery:
        """Query builder over the newest deliveries."""
        return cls.query().order_by((cls.c.received_at, "DESC")).limit(limit)


__all__ = [
    "EVENT_WORKFLOW_JOB",
    "EVENT_WORKFLOW_RUN",
    "REASON_IGNORED",
    "REASON_INVALID_JSON",
    "REASON_PROCESSED",
    "REASON_SIGNATURE_FAILED",
    "REASON_UNSUPPORTED_EVENT",
    "AsyncWebhookDelivery",
    "WebhookDelivery",
]
