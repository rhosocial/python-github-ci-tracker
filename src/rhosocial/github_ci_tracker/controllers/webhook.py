# src/rhosocial/github_ci_tracker/controllers/webhook.py
"""The webhook controller: the push channel, end to end.

A controller is a group of flows. :class:`WebhookController` covers the whole
push channel -- create and remove hooks, verify a signature, apply one delivery,
record its outcome. :class:`DeliveryLedger` below is the ledger those flows write
to: every delivery that reaches the receiver gets a row, accepted or not, which
is what answers "is the push channel alive?" and "was this delivery handled?".

A delivery is a sequence of model operations, not a unit of work: the run, its
attempt, the job and its steps are each written through the model that owns
them, and each write is complete on its own. If the sequence stops halfway, the
rows it reached stay -- the next read asks GitHub, which is the source for
everything that can change, and repairs the rest. The ledger records that this
delivery was not handled, so a gap is visible instead of silent.

Two steps do matter:

1. **Adopt before writing** -- a ``workflow_job`` event can arrive before its
   run, and adopting it is a network call; it must not sit between the writes.
2. **The ledger always gets a row** -- accepted, ignored or malformed. A failure
   to write it must never turn a valid delivery into a 500, because GitHub would
   retry and the retry double-handles the event.

The outcome codes live here rather than in the model because they are what the
controller writes; the model only stores them."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..core.exceptions import (
    CitrackerError,
    RepositoryNotFoundError,
    ValidationError,
    WebhookNotFoundError,
)
from ..models.webhook_delivery import (
    EVENT_WORKFLOW_JOB,
    EVENT_WORKFLOW_RUN,
    REASON_IGNORED,
    REASON_INVALID_JSON,
    REASON_PROCESSED,
    REASON_SIGNATURE_FAILED,
    REASON_UNSUPPORTED_EVENT,
    AsyncWebhookDelivery,
)
from ..services.github.protocols import RunReader, WebhookManager
from ..views import ENV_WEBHOOK_SECRET, ENV_WEBHOOK_URL, _clean_str, _webhook_defaults
from .run import RunController

if TYPE_CHECKING:
    from ..core.run_tracker import RunTracker
    from ..models import AsyncJob, AsyncRepository, AsyncWorkflowRun
    from .logs import RunLogCapture
    from .repository import RepositoryData
    from .run import RunData

__all__ = [
    "DEFAULT_WEBHOOK_EVENTS",
    "RECORDED_EVENTS",
    "EVENT_WORKFLOW_JOB",
    "EVENT_WORKFLOW_RUN",
    "REASON_IGNORED",
    "REASON_INVALID_JSON",
    "REASON_PROCESSED",
    "REASON_SIGNATURE_FAILED",
    "REASON_UNSUPPORTED_EVENT",
    "WebhookController",
    "DeliveryLedger",
]


logger = logging.getLogger(__name__)

#: The event streams this service dispatches. Anything else is recorded in the
#: ledger as ``unsupported_event`` rather than dropped silently: the delivery
#: still proves the channel is alive.
RECORDED_EVENTS = frozenset({"workflow_run", "workflow_job"})

#: Events a webhook subscribes to by default.
DEFAULT_WEBHOOK_EVENTS: tuple[str, ...] = ("workflow_run", "workflow_job")


class WebhookController:
    """Manage GitHub webhooks and process incoming deliveries.

    Args:
        webhooks: The hooks this service owns.
        readers: Run and log reads, used to adopt a run whose job event arrived
            first and to fetch a failed job's log.
        runs: The read path that adopts or updates a run.
        repositories: Repository rows, looked up by the payload's ids.
        runs_data: Run and job rows.
        deliveries: The delivery ledger.
        tracker: The tracker shared with the dispatch path. A run this service
            started is the one a delivery is expected for.
        database: The process's database seam, used by the eager log fetch of a
            failed job: that fetch outlives the request, so it takes a connection
            of its own rather than borrowing the one the response is about to
            return.
    """

    def __init__(
        self,
        *,
        webhooks: WebhookManager,
        readers: RunReader,
        runs: RunController,
        repositories: RepositoryData,
        runs_data: RunData,
        deliveries: DeliveryLedger,
        tracker: RunTracker | None = None,
        log_scope: Any = None,
        logs: RunLogCapture | None = None,
    ) -> None:
        self._webhooks = webhooks
        self._readers = readers
        self._runs = runs
        self._repos = repositories
        self._store = runs_data
        self._ledger = deliveries
        self._tracker = tracker
        self._log_tasks: set[asyncio.Task[None]] = set()
        #: How the eager log fetch of a failed job gets a connection. Supplied by
        #: the application, which owns connections; ``None`` skips the fetch.
        self._log_scope = log_scope
        #: Step-log capture. A completion delivery is the earliest moment GitHub
        #: still has the per-step files, so it is the moment to take them.
        self._logs = logs

    # -- hooks -----------------------------------------------------------------

    async def create_webhook(
        self,
        repo_full_name: str,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
        events: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create the CI webhook of a repository.

        The URL and secret default to ``CITRACKER_WEBHOOK_URL`` and
        ``CITRACKER_WEBHOOK_SECRET``.

        Raises:
            ValidationError: Neither the arguments nor the environment supply a
                URL and a secret.
        """
        defaults = _webhook_defaults()
        url = _clean_str(webhook_url) or defaults["url"]
        secret = _clean_str(webhook_secret) or defaults["secret"]
        if not url or not secret:
            raise ValidationError(
                "Webhook URL and secret are required (pass them or set "
                f"{ENV_WEBHOOK_URL} / {ENV_WEBHOOK_SECRET})"
            )
        created = await self._webhooks.create_webhook(
            repo_full_name,
            {
                "url": url,
                "content_type": "json",
                "secret": secret,
                "events": list(events or DEFAULT_WEBHOOK_EVENTS),
            },
        )
        return dict(created)

    async def delete_webhook(self, repo_full_name: str) -> int:
        """Delete every webhook pointing at this service.

        Returns:
            Number of hooks deleted.
        """
        url = _webhook_defaults()["url"]
        deleted = 0
        for hook in await self.list_webhooks(repo_full_name):
            config = hook.get("config") or {}
            if url and _clean_str(config.get("url")) != url:
                continue
            hook_id = int(hook.get("id") or 0)
            if not hook_id:
                continue
            await self._webhooks.delete_webhook(repo_full_name, hook_id)
            deleted += 1
        return deleted

    async def list_webhooks(self, repo_full_name: str) -> list[dict[str, Any]]:
        """List the webhooks configured on a repository."""
        return [dict(hook) for hook in await self._webhooks.list_webhooks(repo_full_name)]

    async def ping_webhook(self, repo_full_name: str) -> dict[str, Any]:
        """Send a test ping to this service's webhook of a repository.

        Raises:
            RepositoryNotFoundError: The repository is not tracked.
            WebhookNotFoundError: No hook of this service is configured there.
        """
        if await self._repos.by_name(repo_full_name) is None:
            raise RepositoryNotFoundError(f"Repository '{repo_full_name}' is not tracked")

        url = _webhook_defaults()["url"]
        for hook in await self.list_webhooks(repo_full_name):
            config = hook.get("config") or {}
            if url and _clean_str(config.get("url")) != url:
                continue
            hook_id = int(hook.get("id") or 0)
            if hook_id:
                await self._webhooks.ping_webhook(repo_full_name, hook_id)
                return {"hook_id": hook_id, "url": _clean_str(config.get("url")), "pinged": True}

        raise WebhookNotFoundError(f"No webhook for this service on repository '{repo_full_name}'")

    # -- intake ----------------------------------------------------------------

    async def process_webhook(
        self,
        event_type: str,
        payload: bytes | str | Mapping[str, Any],
        delivery_id: str = "",
    ) -> dict[str, Any]:
        """Verify and dispatch a webhook delivery.

        Args:
            event_type: Value of ``X-GitHub-Event``.
            payload: Raw body (bytes/str) or an already parsed mapping.
            delivery_id: Value of ``X-GitHub-Delivery``; empty when absent.

        Returns:
            ``{"handled": bool, "reason": str}``.
        """
        data: Mapping[str, Any]
        if isinstance(payload, Mapping):
            data = payload
        else:
            body = payload.decode("utf-8") if isinstance(payload, bytes) else payload
            try:
                parsed = json.loads(body)
            except (ValueError, TypeError) as exc:
                return await self.record_delivery(
                    event_type=event_type,
                    delivery_id=delivery_id,
                    reason=REASON_INVALID_JSON,
                    detail=f"Invalid JSON payload: {exc}",
                )
            if not isinstance(parsed, Mapping):
                # Valid JSON that is not an object. The handlers all expect a
                # mapping, and letting it through turns into an AttributeError
                # deeper in.
                return await self.record_delivery(
                    event_type=event_type,
                    delivery_id=delivery_id,
                    reason=REASON_INVALID_JSON,
                    detail=f"Payload root is {type(parsed).__name__}, not an object",
                )
            data = parsed

        if event_type == "ping":
            return await self.record_delivery(
                event_type=event_type,
                delivery_id=delivery_id,
                payload=data,
                handled=True,
                reason=REASON_PROCESSED,
            )

        if event_type not in RECORDED_EVENTS:
            return await self.record_delivery(
                event_type=event_type,
                delivery_id=delivery_id,
                payload=data,
                handled=False,
                reason=REASON_UNSUPPORTED_EVENT,
            )

        try:
            # The adoption reads the parent run from GitHub; it happens first so
            # that network time is not spent between the writes below.
            await self.ensure_parent_run(event_type, data)
            handled = await self.handle_event(event_type, data)
        except Exception as exc:  # pragma: no cover - defensive
            return await self.record_delivery(
                event_type=event_type,
                delivery_id=delivery_id,
                payload=data,
                handled=False,
                reason=type(exc).__name__,
                detail=str(exc),
            )

        return await self.record_delivery(
            event_type=event_type,
            delivery_id=delivery_id,
            payload=data,
            handled=handled,
            reason=REASON_PROCESSED if handled else REASON_IGNORED,
        )

    async def record_delivery(
        self,
        event_type: str,
        delivery_id: str = "",
        payload: Mapping[str, Any] | None = None,
        handled: bool = False,
        reason: str = "",
        detail: str = "",
    ) -> dict[str, Any]:
        """Write one delivery to the ledger and answer in the handler's shape.

        Separate from :meth:`process_webhook` so the receiver can record a
        delivery it rejected before any handler ran -- a failed signature is
        exactly the case an operator needs to see.
        """
        await self._ledger.record(
            delivery_id=delivery_id,
            event_type=event_type,
            reason=reason,
            handled=handled,
            payload=payload,
            detail=detail,
        )
        return {"handled": handled, "reason": detail or reason}

    # -- event handling --------------------------------------------------------

    async def record_signature_failure(
        self, event_type: str, delivery_id: str = ""
    ) -> dict[str, Any]:
        """Record a delivery the receiver rejected for a bad signature.

        Named for the reason instead of taking one: the receiver knows *that* the
        signature did not verify, and how a rejection is spelled in the ledger is
        this layer's business. Recording it before raising is what separates
        "GitHub says it delivered but nothing arrived" from "it arrived and was
        refused" -- the ledger is the only place that difference is visible.
        """
        return await self.record_delivery(
            event_type=event_type,
            delivery_id=delivery_id,
            handled=False,
            reason=REASON_SIGNATURE_FAILED,
        )

    async def handle_event(self, event_type: str, payload: Mapping[str, Any]) -> bool:
        """Dispatch a delivery to its handler.

        Returns:
            ``True`` when the delivery was applied, ``False`` when it was ignored
            (unknown type, unsubscribed repository, unknown parent run).
        """
        if event_type == "workflow_run":
            return await self.handle_workflow_run(payload)
        if event_type == "workflow_job":
            return await self.handle_workflow_job(payload)
        logger.debug("Ignoring unsupported webhook event '%s'", event_type)
        return False

    async def handle_workflow_run(self, payload: Mapping[str, Any]) -> bool:
        """Handle a ``workflow_run`` event.

        The subscription is the authorization, not the trigger: a run of a
        subscribed repository is stored whether it came from a push, a pull
        request, another actor or this service -- that is the whole point of the
        push channel. An unsubscribed repository is still refused, and the
        delivery is recorded as unhandled.
        """
        run_data = payload.get("workflow_run")
        if not isinstance(run_data, Mapping):
            return False
        github_run_id = _int(run_data.get("id"))
        if not github_run_id:
            logger.debug("workflow_run event without a run id, ignored")
            return False

        repo = await self._repository(payload)
        if repo is None:
            logger.debug("Ignoring workflow_run %s: repository is not subscribed", github_run_id)
            return False

        action = _clean_str(payload.get("action"), "unknown")
        run = await self._store.run(github_run_id)
        if run is None:
            try:
                run = await self._store.adopt(repo, run_data)
            except Exception:  # noqa: BLE001 - a concurrent delivery created it first
                # Two deliveries of one run can land together ("queued" and
                # "completed" are seconds apart on a fast job): both read "no
                # row", both insert, and the loser hits the unique constraint.
                # Re-read and treat the payload as an update.
                existing = await self._store.run(github_run_id)
                if existing is None:
                    raise
                run = await self._store.update(existing, run_data)
        else:
            run = await self._store.update(run, run_data)

        await self._store.note_webhook_received(run)

        logger.info(
            "workflow_run %s: %s run#%s -> %s/%s",
            action,
            repo.full_name,
            run.run_number,
            run.latest_status,
            run.latest_conclusion,
        )
        if run.latest_status == "completed" and self._tracker is not None:
            # Terminal state: stop tracking, so the tracker stays bounded.
            self._tracker.remove(github_run_id)
        if run.latest_status == "completed" and self._logs is not None:
            # Best effort, and deliberately not fatal: the delivery is already
            # stored, and a capture that fails now can still be retried by a
            # read while the archive is hot. Turning it into an exception would
            # mark the delivery unhandled for something the read path repairs.
            try:
                await self._logs.ensure(repo, run)
            except Exception as exc:  # noqa: BLE001 - a delivery must not fail on this
                logger.warning(
                    "Step-log capture for run %s failed: %s: %s",
                    github_run_id,
                    type(exc).__name__,
                    exc,
                )
        return True

    async def handle_workflow_job(self, payload: Mapping[str, Any]) -> bool:
        """Handle a ``workflow_job`` event.

        Returns:
            ``True`` when the job was stored, ``False`` when its parent run is
            unknown and could not be adopted.
        """
        job_data = payload.get("workflow_job")
        if not isinstance(job_data, Mapping):
            return False
        github_job_id = _int(job_data.get("id"))
        github_run_id = _int(job_data.get("run_id"))
        if not github_job_id or not github_run_id:
            logger.debug("workflow_job event without ids, ignored")
            return False

        # ``with_state`` because the job's attempt is resolved from the run's
        # latest attempt: a run loaded without it resolves attempt 0 and files
        # the job under a row of its own, leaving the real one stale.
        run = await self._store.run(github_run_id, with_state=True)
        if run is None:
            # Job events can outrun the run event: GitHub sends one per job, and
            # on a busy matrix the first may arrive before the run is known.
            run = await self._adopt_run_for_job(github_run_id, payload)
        if run is None:
            logger.debug(
                "Ignoring workflow_job %s: parent run %s is unknown and could not be adopted",
                github_job_id,
                github_run_id,
            )
            return False

        # The job's model upsert resolves its attempt and cascades the payload's
        # steps, so the controller hands over the payload and reads the row back.
        await self._store.sync_jobs(run, [job_data])
        job = await self._store.job(github_job_id)
        if job is None:  # pragma: no cover - the upsert just stored it
            return False

        if job.is_finished and not job_data.get("steps"):
            # A completion payload that carries no steps updates the job and
            # leaves its steps at whatever the running payload said -- observed
            # on a real run as steps stuck at in_progress under a successful job.
            # One read of this job fixes it; the list endpoint would fetch the
            # whole matrix for the same information.
            repo = await self._repository(payload)
            if repo is not None:
                try:
                    remote = await self._readers.get_job(repo.full_name, github_job_id)
                    await self._store.sync_jobs(run, [remote])
                    job = await self._store.job(github_job_id) or job
                except CitrackerError as exc:
                    logger.warning(
                        "Could not read job %s back: %s: %s",
                        github_job_id,
                        type(exc).__name__,
                        exc,
                    )

        logger.info(
            "workflow_job %s: %s (run %s) -> %s/%s",
            _clean_str(payload.get("action"), "unknown"),
            job.name,
            github_run_id,
            job.status,
            job.conclusion,
        )
        if job.is_failed:
            self._schedule_log_fetch(run, job)
        # No step-log capture here. The archive is per run, and GitHub answers
        # ``404 Not Found`` for it until the whole run finishes -- measured on a
        # 12-job matrix, one 404 per job completion. Capture belongs to the run's
        # completion and to reads inside the window; a job completion is neither.
        return True

    async def ensure_parent_run(self, event_type: str, payload: Mapping[str, Any]) -> None:
        """Make sure the run a ``workflow_job`` event belongs to is stored.

        Called by the receiver *before* it writes the job, because the adoption is
        a network call. Idempotent and cheap when the run exists: one indexed
        lookup. A run that cannot be adopted simply stays unknown, and the
        delivery is recorded as unhandled; ``find_runs`` (which reads GitHub)
        repairs it later.
        """
        if event_type != "workflow_job":
            return
        job_data = payload.get("workflow_job")
        if not isinstance(job_data, Mapping):
            return
        github_run_id = _int(job_data.get("run_id"))
        if not github_run_id:
            return
        if await self._store.run(github_run_id) is not None:
            return
        await self._adopt_run_for_job(github_run_id, payload)

    # -- internals -------------------------------------------------------------

    async def _repository(self, payload: Mapping[str, Any]) -> AsyncRepository | None:
        """The subscribed repository a payload names, or ``None``.

        Only already-subscribed repositories are returned: a delivery for an
        unknown one belongs to a run this system never tracked, and creating a
        row here would be dead data.
        """
        repo_data = payload.get("repository")
        if not isinstance(repo_data, Mapping):
            logger.debug("webhook payload without repository information, ignored")
            return None

        github_repo_id = _int(repo_data.get("id"))
        full_name = _clean_str(repo_data.get("full_name"))
        if not github_repo_id and not full_name:
            return None

        repo = await self._repos.by_github_id(github_repo_id) if github_repo_id else None
        if repo is None and full_name:
            repo = await self._repos.by_name(full_name)
        return repo

    async def _adopt_run_for_job(
        self, github_run_id: int, payload: Mapping[str, Any]
    ) -> AsyncWorkflowRun | None:
        """Fetch and store the run a ``workflow_job`` event belongs to.

        Job events carry ``run_id`` but not the run's own fields, so the run is
        read from GitHub. Two ways this ends without a row, both silent: the
        repository is not subscribed, or GitHub refuses the read. The next job
        delivery of the same run retries, and the run event creates the row when
        it arrives.
        """
        existing = await self._store.run(github_run_id)
        if existing is not None:
            # Every job of a run emits its own delivery, and they arrive
            # together: only the first needs to read the run from GitHub.
            return existing

        repo = await self._repository(payload)
        if repo is None:
            logger.debug(
                "Ignoring workflow_job for run %s: repository is not subscribed", github_run_id
            )
            return None

        try:
            run = await self._runs.fetch(repo, github_run_id, with_jobs=False)
        except CitrackerError as exc:
            logger.warning(
                "Could not adopt run %s of '%s' for its job event: %s",
                github_run_id,
                repo.full_name,
                exc,
            )
            return None

        logger.info(
            "Adopted run %s of '%s' from a job event that arrived first",
            github_run_id,
            repo.full_name,
        )
        return run

    def _schedule_log_fetch(self, run: AsyncWorkflowRun, job: AsyncJob) -> None:
        """Fetch the logs of a failed job without blocking the delivery.

        The task starts in a **fresh context** and gets its own connection
        scope. A task created inside the request would inherit its ContextVars --
        including the pooled connection the models were bound to -- and would
        keep using it after the response returned that connection to the pool.
        """
        if self._log_scope is None:
            logger.debug(
                "No connection scope configured; skipping the eager log fetch for job %s", job.id
            )
            return

        def start() -> None:
            try:
                task = asyncio.create_task(self._fetch_in_scope(run, job))
            except RuntimeError:  # pragma: no cover - no running loop
                logger.debug("No running loop; skipping log fetch for job %s", job.id)
                return
            self._log_tasks.add(task)
            task.add_done_callback(self._log_tasks.discard)

        # ``Context.run`` makes ``create_task`` capture the empty context.
        contextvars.Context().run(start)

    async def _fetch_in_scope(self, run: AsyncWorkflowRun, job: AsyncJob) -> None:
        """Run the fetch inside a scope of its own, on its own connection."""
        assert self._log_scope is not None
        async with self._log_scope():
            await self._fetch_job_logs(run, job)

    async def _fetch_job_logs(self, run: AsyncWorkflowRun, job: AsyncJob) -> None:
        """Fetch and store the logs of a failed job (best effort).

        The credential is the group's business: the repository the run belongs to
        is enough to ask, and an anonymous repository simply gets a refusal from
        GitHub that is logged rather than raised.
        """
        try:
            repo = await self._store.repository_of(run)
            if repo is None:
                return
            content = await self._readers.get_job_logs(repo.full_name, job.github_job_id)
            await self._store.store_log(job, content)
            logger.info(
                "Stored %d byte(s) of logs for job %s of '%s'",
                len(content),
                job.github_job_id,
                repo.full_name,
            )
        except CitrackerError as exc:
            logger.warning("Could not fetch logs of job %s: %s", job.github_job_id, exc)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Unexpected error while fetching logs of job %s: %s", job.github_job_id, exc
            )


def _int(value: Any) -> int:
    """Coerce a GitHub scalar to ``int``, using ``0`` when it is not one."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# -- the rows the flows above are built from ------------------------------


class DeliveryLedger:
    """The delivery ledger, one method per question the receiver asks."""

    async def is_redelivery(self, delivery_id: str) -> bool:
        """Whether this ``X-GitHub-Delivery`` value is already in the ledger.

        A redelivery is GitHub retrying, which is different from two distinct
        deliveries and worth telling apart when diagnosing a duplicate update.
        """
        if not delivery_id:
            return False
        try:
            count = (
                await AsyncWebhookDelivery.query()
                .where(AsyncWebhookDelivery.c.delivery_id == delivery_id)
                .count()
            )
        except Exception:  # noqa: BLE001 - the ledger must never break intake
            return False
        return count > 0

    async def latest_received(self, event_type: str) -> datetime | None:
        """When this stream's newest delivery arrived, or ``None`` if never.

        The question "is the channel alive?" is answered by the arrival time of
        the newest delivery, not by its outcome: a delivery this service ignored
        still proves GitHub reached it.
        """
        row = await (
            AsyncWebhookDelivery.query()
            .select(AsyncWebhookDelivery.c.received_at)
            .where(AsyncWebhookDelivery.c.event_type == event_type)
            .order_by((AsyncWebhookDelivery.c.received_at, "DESC"))
            .limit(1)
            .one()
        )
        return None if row is None else row.received_at

    async def count(
        self,
        event_type: str,
        *,
        since: datetime | None = None,
        redelivered: bool | None = None,
    ) -> int:
        """How many deliveries of this stream match the optional filters.

        Args:
            event_type: The GitHub event stream.
            since: Only deliveries at or after this instant.
            redelivered: Only (or only non-) GitHub retries.
        """
        query = AsyncWebhookDelivery.query().where(AsyncWebhookDelivery.c.event_type == event_type)
        if since is not None:
            query = query.where(AsyncWebhookDelivery.c.received_at >= since)
        if redelivered is not None:
            query = query.where(AsyncWebhookDelivery.c.is_redelivery == redelivered)
        return await query.count()

    async def unhandled_since(self, event_type: str, since: datetime) -> list[AsyncWebhookDelivery]:
        """The deliveries of this stream that arrived and were not handled.

        Returned as rows, not counts: the *reason* is what separates "a foreign
        repository we ignore on purpose" from "our own write failed", and that
        classification is the caller's.
        """
        return await (
            AsyncWebhookDelivery.query()
            .where(
                (AsyncWebhookDelivery.c.event_type == event_type)
                & (AsyncWebhookDelivery.c.handled == False)  # noqa: E712
                & (AsyncWebhookDelivery.c.received_at >= since)
            )
            .all()
        )

    async def record(
        self,
        *,
        delivery_id: str,
        event_type: str,
        reason: str,
        handled: bool,
        payload: Mapping[str, object] | None = None,
        detail: str = "",
    ) -> None:
        """Write one delivery to the ledger.

        Total against shape: a payload whose top level is not an object, or whose
        ``repository`` is not one, must not take the recording down with it. The
        delivery happened, and "it happened with a body we could not read" is
        exactly what the ledger exists to say. Nothing here raises -- a failure
        would turn a valid delivery into a 500, and GitHub would retry it into a
        double-handled event.
        """
        data = payload if isinstance(payload, Mapping) else {}
        repository = data.get("repository")
        repository = repository if isinstance(repository, Mapping) else {}
        run_data = data.get("workflow_run")
        run_data = run_data if isinstance(run_data, Mapping) else {}
        job_data = data.get("workflow_job")
        job_data = job_data if isinstance(job_data, Mapping) else {}

        action = data.get("action")
        try:
            await AsyncWebhookDelivery(
                delivery_id=delivery_id,
                event_type=event_type,
                received_at=datetime.now(timezone.utc),
                repository_full_name=str(repository.get("full_name") or ""),
                action=str(action)[:32] if isinstance(action, str) and action else None,
                github_run_id=(
                    _optional_int(run_data.get("id")) or _optional_int(job_data.get("run_id"))
                ),
                github_job_id=_optional_int(job_data.get("id")),
                handled=handled,
                reason=(f"{reason}: {detail}" if detail else reason)[:64],
                is_redelivery=await self.is_redelivery(delivery_id),
            ).save()
        except Exception:  # noqa: BLE001 - the ledger must never break intake
            import logging

            logging.getLogger(__name__).exception(
                "could not record webhook delivery %s", delivery_id
            )


def _optional_int(value: object) -> int | None:
    """Coerce a GitHub scalar to ``int``, or ``None`` when it is not one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
