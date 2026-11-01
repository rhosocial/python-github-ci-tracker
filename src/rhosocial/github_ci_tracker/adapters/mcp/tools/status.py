# src/rhosocial/github_ci_tracker/adapters/mcp/tools/status.py
"""``ci_status``: the tool that tells a model what it is allowed to do.

Calling it once at the start replaces guessing from error messages. It reports
the service version and the build it was made from, the identity behind each
credential, what the caller may do (``can_trigger`` and, when allowed, which
workflows), the limits that shape its answers (log window, page size, wait
budget) and the health signals that explain missing data (when a webhook was
last delivered).

The capability answer is measured, not declared: ``can_trigger`` is true only
when a credential is bound to the repository and that credential carries
Actions write. A read-only token therefore cannot be talked into triggering,
no matter what the caller asks for.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mcp.server import MCPServer

from ....build import build_info
from ....views import StreamHealth
from ..logs import DEFAULT_TAIL, MAX_CHARS
from ..result import READ_ONLY, error_envelope, settings_url
from ..tools.query import MAX_LIMIT, MAX_WAIT_SECONDS


def _service_version() -> str:
    """The installed package version, or a placeholder when not installed."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("rhosocial-github-ci-tracker")
        except PackageNotFoundError:
            return "unknown"
    except Exception:  # pragma: no cover - metadata is best effort
        return "unknown"


async def _health_snapshot(container: Any) -> dict[str, Any]:
    """Describe whether the push channel is alive, per GitHub event stream.

    GitHub treats ``workflow_run`` and ``workflow_job`` as separate streams --
    separate payloads, separate lifecycles -- so they are reported separately
    rather than merged into one number. Each stream reports the last delivery,
    how many arrived in the last hour, the total, and how many were redeliveries.

    The figures and the alert classification are the status service's; this
    function spells them as the JSON a model reads, and adds the one sentence it
    can act on when a delivery was received and not stored.
    """
    snapshot = await container.status.snapshot()
    pool = _pool_payload(container)

    if not snapshot.counts_available:
        return {
            "available": True,
            "ledger_available": False,
            "error": snapshot.error,
            "pool": pool,
        }

    if not snapshot.ledger_available:
        return {
            "available": True,
            "ledger_available": False,
            "pool": pool,
            "repository_count": snapshot.repository_count,
            "runs_tracked": snapshot.runs_tracked,
            "error": snapshot.error,
            "stale_data_hint": (
                "The delivery ledger is missing (created by the current schema). "
                "Re-create it with `rhosocial-github-ci-tracker-db init`; until "
                "then webhook health cannot be measured -- find_runs reads GitHub "
                "either way, so run data is still correct."
            ),
        }

    # A delivery we failed to store is not "unhandled, so what": the run or job
    # it described is missing from this database, and nothing else will notice.
    # Promoted out of the histogram so a caller can act on it without knowing
    # which reason codes are expected.
    alerts = [
        {
            **alert,
            "runs": (
                snapshot.streams[alert["stream"]].affected_runs
                if alert.get("stream") in snapshot.streams
                else []
            ),
            "hint": (
                "The delivery arrived but storing what it described failed, so a "
                "run or job is missing or stale. `runs` lists the affected ids: "
                "call get_run on each and the read repairs it from GitHub, or call "
                "find_runs to sweep a whole repository. The "
                "ledger rows behind them are readable with "
                "`rhosocial-github-ci-tracker-db deliveries --unhandled`, and this "
                "alert clears itself once they fall out of the 24-hour window."
            ),
        }
        for alert in snapshot.alerts
    ]

    return {
        "available": True,
        "ledger_available": True,
        "repository_count": snapshot.repository_count,
        "runs_tracked": snapshot.runs_tracked,
        "pool": pool,
        "alerts": alerts,
        # Kept for callers written against the earlier shape. Both are now
        # derived from the ledger: the run-level one from the workflow_run
        # stream, the count from every delivery.
        "last_webhook_at": _iso(snapshot.last_webhook_at),
        "webhooks_in_last_hour": snapshot.webhooks_in_last_hour,
        "push_channel": snapshot.push_channel,
        "ingest": snapshot.ingest,
        "streams": {
            event_type: _stream_payload(health) for event_type, health in snapshot.streams.items()
        },
        "stale_data_hint": (
            "If a run looks missing, call find_runs; that path " "does not depend on webhooks."
        ),
    }


def _pool_payload(container: Any) -> dict[str, Any] | None:
    """The connection pool's current numbers, or ``None`` when there is no pool.

    A pool that has lost its permits looks healthy from outside: every request
    waits out the 30-second acquire and answers 500, while ``/readyz`` is
    reported separately. The two figures that gave away the 2026-10-30 incident
    were ``available`` and ``in_use`` both reaching zero with ``max_size`` above
    zero -- a pool no request could ever leave. Reported here because this is the
    one call a caller makes to ask "is the service well".
    """
    database = getattr(container, "database", None)
    pool = getattr(database, "pool", None) if database is not None else None
    if pool is None:
        return None
    try:
        stats = pool.get_stats()
        max_size = int(getattr(pool.config, "max_size", 0) or 0)
    except Exception as exc:  # noqa: BLE001 - a report, never an error
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}

    payload: dict[str, Any] = {
        "in_use": stats.current_in_use,
        "available": stats.current_available,
        "max_size": max_size,
        "acquire_timeouts": stats.total_timeouts,
        "acquires": stats.total_acquired,
        "uptime_seconds": int(stats.uptime),
    }
    # The reading itself holds one connection, so "none in use" never appears
    # from here: a lost-permit pool shows up as zero available with at most this
    # one lease.
    if max_size > 1 and stats.current_available == 0 and stats.current_in_use <= 1:
        # No connection to spare while at most the caller's own is checked out:
        # permits were lost, not held. Waiting will not help, and no request can
        # recover a pool in this state.
        payload["hint"] = (
            "No connection is available and at most this request's own is in use, "
            "while max_size is above one: permits were lost, not held. Restart "
            "the service; waiting cannot recover a pool in this state."
        )
    elif max_size and stats.current_in_use >= max_size:
        payload["hint"] = (
            f"Every one of the {max_size} connections is checked out. Long waits "
            "hold a connection for their whole duration, so new requests will "
            "queue and eventually time out at acquire."
        )
    return payload


def _iso(value: datetime | None) -> str | None:
    """A timestamp as the wire spells it."""
    return None if value is None else value.isoformat()


def _stream_payload(health: StreamHealth) -> dict[str, Any]:
    """One event stream's figures, timestamps as ISO strings."""
    failure = health.last_write_failure
    return {
        "last_at": _iso(health.last_at),
        "in_last_hour": health.in_last_hour,
        "deliveries_total": health.deliveries_total,
        "redelivered": health.redelivered,
        "unhandled": health.unhandled,
        "unhandled_reasons": health.unhandled_reasons,
        "write_failures": health.write_failures,
        "last_write_failure": (
            None
            if failure is None
            else {**failure, "received_at": _iso(failure.get("received_at"))}
        ),
        # The ids a repair pass needs; a count alone cannot name them.
        "affected_runs": health.affected_runs,
    }


def _trigger_scope(
    names: list[str],
    workflows_by_repository: dict[str, list[Any]],
    *,
    writable: bool,
) -> list[dict[str, Any]]:
    """Report which repositories may be dispatched.

    One gate: the bound credential must carry Actions write. Nothing else
    authorizes or refuses a dispatch, so reporting anything more would invent a
    second gate that does not exist.

    Args:
        names: ``owner/repo`` names to report on.
        workflows_by_repository: Each repository's workflows, as the service
            returned them (an empty list where none are known).
        writable: Whether any active credential measured Actions write.

    Returns:
        One entry per repository, each listing its known workflows.
    """
    return [
        {
            "repository": name,
            "trigger_allowed": writable,
            "reason": (
                None if writable else "the bound credential has no Actions write permission"
            ),
            "workflows": [
                {"path": workflow.path, "name": workflow.name}
                for workflow in workflows_by_repository.get(name, [])
            ],
        }
        for name in names
    ]


def register_status_tools(server: MCPServer, container: Any) -> None:
    """Register the status MCP tool."""

    @server.tool(
        name="ci_status",
        description=(
            "Describe this service and what you may do with it: identity, tracked "
            "repositories, whether triggering is possible and for which workflows, "
            "the limits that shape results (log window, page size, wait budget) and "
            "health signals. Call it once when unsure what is permitted, instead of "
            "discovering limits by triggering errors. It never changes anything."
        ),
        annotations=READ_ONLY,
    )
    async def ci_status(
        repository: str | None = None,
        include_repositories: bool = True,
    ) -> dict[str, Any]:
        """Report identity, capabilities, limits and health.

        Args:
            repository: Optional ``owner/repo`` to report capabilities for one
                repository specifically.
            include_repositories: List the tracked repositories (the identities
                are always reported).

        Returns:
            A self-description: ``mode``, ``identity``, ``capabilities``,
            ``limits`` and ``health``.
        """

        try:
            tokens = await container.token.list_tokens()
            service = container.repository
            repos = await service.list_repositories()

            identities = [
                {
                    "credential": token["credential"],
                    "owner_login": token["owner_login"],
                    "is_active": token["is_active"],
                    "expires_at": token["expires_at"],
                    # Measured, not configured: this is the credential's own
                    # probe result.
                    "can_write_actions": token.get("can_write_actions"),
                }
                for token in tokens
            ]

            active = [item for item in identities if item["is_active"]]
            repositories = [
                {
                    "full_name": repo.full_name,
                    "private": repo.is_private,
                    "default_branch": repo.default_branch,
                }
                for repo in repos
            ]

            # A repository with no binding reads anonymously; that is a
            # supported mode, not a fault, so it is reported as a count rather
            # than inferred from is_private.
            bindings = await service.binding_summary()
            repositories_without_binding = bindings.anonymous

            selected = repositories
            if repository is not None:
                selected = [item for item in repositories if item["full_name"] == repository]

            # Triggering needs a bound, active credential AND Actions write on
            # it. With a fine-grained PAT the write scope is the user's choice,
            # so this is a measured capability rather than a product tier -- a
            # read-only token cannot be talked into dispatching.
            writable = any(item.get("can_write_actions") for item in active)

            selected_names = [item["full_name"] for item in selected]
            workflows = await service.workflows_of(selected_names)
            allowed = _trigger_scope(selected_names, workflows, writable=writable)

            can_trigger = any(entry["trigger_allowed"] for entry in allowed)
            can_rerun = can_trigger

            # The "why can I not read?" answer belongs here rather than only in
            # the error of the call that reveals it: a capability of false with
            # no reason reads as a broken service, when it may simply be an
            # operator who has not subscribed a repository yet.
            capability_note = (
                "Repository and credential changes are made by the operator "
                "through the CLI or the REST API, never through these tools."
            )
            settings = settings_url()
            if not repositories and settings:
                capability_note = (
                    "No repository is tracked yet, so every repository-scoped "
                    f"call will report repository_not_subscribed. Add one at {settings}."
                )

            return {
                "mode": "self_hosted",
                "service": {
                    "name": "citracker",
                    "version": _service_version(),
                    # Which build is answering: the commit it was built from and
                    # the CI build number. ``version`` is the same constant in
                    # every build, so this is what answers "is the deployment
                    # current?" -- and ``null`` means the build did not stamp
                    # itself, not that it is old.
                    "build": build_info().as_dict(),
                },
                "protocols": ["mcp", "rest", "cli"],
                "identity": {
                    "credentials": identities,
                    "active_credential_count": len(active),
                    "anonymous_subscriptions": repositories_without_binding,
                },
                "capabilities": {
                    "can_read": bool(repositories),
                    "can_trigger": can_trigger,
                    "can_rerun": can_rerun,
                    "can_manage_repositories": False,
                    "trigger_scope": allowed,
                    "note": capability_note,
                },
                "limits": {
                    "page_size_max": MAX_LIMIT,
                    "log_default_tail_lines": DEFAULT_TAIL,
                    "log_max_chars": MAX_CHARS,
                    "wait_max_seconds": MAX_WAIT_SECONDS,
                    "wait_budget_hint": (
                        "Size wait_for_run's timeout from the workflow's own "
                        "finished runs, not a guess: trigger_workflow reports "
                        "typical_duration_seconds (p50/p90), and a timed-out wait "
                        "reports observed plus suggested_timeout_seconds. A "
                        "workflow with no finished run has neither -- there is "
                        "nothing to sample yet."
                    ),
                },
                "settings_url": settings or None,
                "health": await _health_snapshot(container),
                "repositories": repositories if include_repositories else None,
            }

        except Exception as exc:  # noqa: BLE001 - shaped for the caller
            raise error_envelope(exc) from exc
