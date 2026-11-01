# src/rhosocial/github_ci_tracker/container.py
"""The composition root: what a process builds once, and how it is handed over.

An application is an assembler: it reads settings, opens the database, builds the
controllers, and hands the finished object to whatever serves requests. This
module is that object. A protocol adapter receives a :class:`Container`; it never
builds one, and it never reaches for a process-wide one.

Two entry points, and the difference is deliberate:

* :meth:`Container.build` assembles controllers and nothing else. It is what a
  test, a script and the operator CLI want: no ambient credential, so each
  repository's binding decides which token answers.
* :meth:`Container.start` is :meth:`build` plus the one asynchronous step, reading
  the deployment's fine-grained PAT out of the database. A server calls it inside
  an open connection scope, because that read is a query like any other.

The PAT is resolved once per process on purpose. Rotating it is a database
operation, so a rotation takes effect on restart -- the right trade for a token
that changes rarely, and the reason this object is built at startup rather than
per request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .config import Settings, get_settings
from .controllers import (
    DeliveryLedger,
    DispatchController,
    QueryController,
    RepositoryController,
    RepositoryData,
    RunController,
    RunData,
    RunLogCapture,
    StatusController,
    TokenController,
    TriggerController,
    WebhookController,
    WorkflowController,
)
from .core.interfaces import IGithubClient
from .core.run_tracker import RunTracker
from .credentials import AsyncPatCredential, get_provider
from .credentials.resolver import resolve_for_repository
from .database import Database
from .services.github import (
    GithubCredentials,
    GithubRepositories,
    GithubRuns,
    GithubWebhooks,
)

logger = logging.getLogger(__name__)

#: The provider kind whose credential may accompany a process. A second
#: provider gets its own ambient slot rather than a branch here.
AMBIENT_KIND = "pat"


@dataclass
class Container:
    """The controllers of one process, safe to share across requests.

    The controllers hold no per-request state: the connection is bound by the
    request, not by the controller, and the only mutable member is the
    :class:`RunTracker`, shared between the trigger and webhook controllers on
    purpose -- a run this process started is the one a delivery is expected for.

    ``credential`` is the ambient PAT every GitHub capability was built with, or
    ``None`` when the process routes per repository. It is kept for diagnostics
    only; the credential redacts itself and the token is never printed.

    ``schema_state`` is filled once, by :meth:`start`, from the database it is
    handed. It is a fact about the deployment at startup -- not something to
    re-ask on every readiness probe -- and it is reported without failing the
    probe: a missing table is something to alert on, not something that a load
    balancer can fix by taking the instance out of rotation.
    """

    settings: Settings
    trigger: TriggerController
    query: QueryController
    status: StatusController
    token: TokenController
    repository: RepositoryController
    webhook: WebhookController
    workflow: WorkflowController
    database: Database | None = None
    credential: Any | None = None
    tracker: RunTracker | None = None
    #: ``"ok"``, ``"missing: a, b"``, or ``None`` when nothing has asked yet.
    schema_state: str | None = None

    # -- construction ----------------------------------------------------------

    @classmethod
    def build(
        cls,
        *,
        settings: Settings | None = None,
        database: Database | None = None,
        client: IGithubClient | None = None,
        credential: Any | None = None,
        tracker: RunTracker | None = None,
    ) -> Container:
        """Assemble the controllers, without reading any credential.

        Args:
            settings: The process settings. Defaults to the environment's.
            database: The database seam, when the process has one. The webhook
                controller needs it for the one thing it does after answering:
                the eager log fetch of a failed job, which must take a
                connection of its own.
            client: GitHub client override -- a recording fake in a test.
            credential: The ambient credential, or ``None`` to route per
                repository.
            tracker: Run tracker override. Defaults to a fresh one, which the
                trigger and webhook controllers then share.

        Returns:
            The controllers, ready to be handed to an adapter.
        """
        resolve = None if credential is not None else cls._binding_resolver()
        # One object per capability, handed to every controller that needs it:
        # the grouping is by GitHub resource, not by caller.
        github_runs = GithubRuns(client, credential=credential, resolve=resolve)
        github_repositories = GithubRepositories(client, credential=credential, resolve=resolve)
        github_webhooks = GithubWebhooks(client, credential=credential, resolve=resolve)
        repositories_data = RepositoryData()
        runs_data = RunData()
        ledger = DeliveryLedger()
        shared_tracker = tracker or RunTracker()

        resolved_settings = settings or get_settings()
        logs = RunLogCapture(
            readers=github_runs,
            runs_data=runs_data,
            settings=resolved_settings.logs,
        )

        runs = RunController(runs=github_runs, runs_data=runs_data)
        dispatch = DispatchController(
            control=github_runs,
            readers=github_runs,
            runs_data=runs_data,
            tracker=shared_tracker,
        )
        query = QueryController(
            readers=github_runs,
            repositories=github_repositories,
            runs=runs,
            runs_data=runs_data,
            logs=logs,
        )
        trigger = TriggerController(dispatch=dispatch, runs=runs)
        repositories = RepositoryController(
            repositories=github_repositories,
            webhooks=github_webhooks,
            readers=github_runs,
            runs=runs,
            data=repositories_data,
            runs_data=runs_data,
            query=query,
            credential=credential,
        )
        return cls(
            settings=resolved_settings,
            trigger=trigger,
            query=query,
            status=StatusController(
                repositories=repositories_data,
                runs=runs_data,
                deliveries=ledger,
            ),
            token=TokenController(probe=GithubCredentials(client, credential=credential)),
            repository=repositories,
            webhook=WebhookController(
                webhooks=GithubWebhooks(client, credential=credential),
                readers=github_runs,
                runs=runs,
                repositories=repositories_data,
                runs_data=runs_data,
                deliveries=ledger,
                tracker=shared_tracker,
                log_scope=database.connection if database is not None else None,
                logs=logs,
            ),
            workflow=WorkflowController(repositories=repositories, trigger=trigger),
            database=database,
            credential=credential,
            tracker=shared_tracker,
        )

    @classmethod
    async def start(
        cls,
        database: Database | None = None,
        *,
        settings: Settings | None = None,
        client: IGithubClient | None = None,
        name: str | None = None,
    ) -> Container:
        """Build the controllers around the deployment's ambient PAT.

        To be called from inside an open connection scope: reading the PAT row
        is a query like any other.
        """
        resolved = settings or get_settings()
        credential = await cls._ambient_credential(resolved, name)
        container = cls.build(
            settings=resolved,
            database=database,
            client=client,
            credential=credential,
        )
        container.schema_state = await container._schema_state()
        return container

    # -- probes ----------------------------------------------------------------

    async def readiness(self) -> dict[str, Any]:
        """Report whether this process can serve a request right now.

        The one check that can change the answer is the database: every flow in
        this service reads or writes a row, so an instance that cannot reach its
        database answers nothing but errors. GitHub is deliberately absent -- a
        third party's outage must not drain every instance, and probing it would
        spend the rate limit the read paths rely on. Whether the ambient PAT
        resolved is absent too: routing per repository is a supported mode, and
        a credential problem is a per-call error, not a reason to stop routing.

        Returns:
            ``{"ready": bool, "checks": {...}}``. The checks are for a human or
            a log line; an orchestrator reads ``ready`` (or the HTTP status the
            adapter derives from it). Nothing here names a host, a user or a
            password.
        """
        checks: dict[str, str] = {"container": "ok"}
        ready = True
        if self.database is None:
            ready = False
            checks["database"] = "not configured"
        else:
            try:
                await self.database.ping()
            except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
                ready = False
                checks["database"] = f"{type(exc).__name__}: {exc}"
            else:
                checks["database"] = "ok"
        if self.schema_state is not None:
            checks["schema"] = self.schema_state
        return {"ready": ready, "checks": checks}

    async def healthy(self) -> bool:
        """``True`` when this process can serve; the MCP layer's health callback."""
        report = await self.readiness()
        return bool(report["ready"])

    async def _schema_state(self) -> str:
        """Ask once, at startup, whether every table of the schema exists.

        ``SELECT 1`` proves the database answers; it does not prove the tables
        are there. This is the one question cheap enough to ask once and wrong
        to ask on every probe (an ``information_schema`` read every ten seconds
        per instance). A missing table is reported in the readiness body and
        logged as an error, but it does not fail the probe: ``init`` owns
        creating the schema, and draining the instance would not create it.
        """
        if self.database is None:
            return "not checked (no database)"
        from .schema import missing_tables

        try:
            async with self.database.connection() as backend:
                missing = await missing_tables(backend)
        except Exception as exc:  # noqa: BLE001 - a degraded start is still a start
            logger.warning("Could not read the schema state: %s: %s", type(exc).__name__, exc)
            return f"unknown ({type(exc).__name__})"
        if missing:
            logger.error(
                "The database is missing %d table(s): %s. Run "
                "`rhosocial-github-ci-tracker-db init`.",
                len(missing),
                ", ".join(missing),
            )
            return f"missing: {', '.join(missing)}"
        return "ok"

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    async def _ambient_credential(settings: Settings, name: str | None) -> Any | None:
        """Read the process's fine-grained PAT, or ``None`` when there is none.

        The rows are managed by ``docker/init.d`` (``credential pat ensure``) and
        by ``credential rotate``, both of which store what GitHub answered when
        the token was probed. Resolving the row here -- once, at startup -- is
        what preserves ``can_write`` / expiry semantics: the token is not trusted
        because it exists, it is trusted because it was verified.

        A missing or unusable PAT must not stop the service from starting:
        rotating it is a database operation, and doing that through the stopped
        service is impossible. Every failure therefore degrades to per-repository
        routing instead of raising.
        """
        alias = name or settings.service.pat_name
        try:
            record = await AsyncPatCredential.find_by_name(alias)
        except Exception as exc:  # noqa: BLE001 - a startup probe must not stop the service
            logger.warning(
                "Could not read the '%s' PAT (%s: %s); controllers resolve credentials "
                "per repository",
                alias,
                exc.__class__.__name__,
                exc,
            )
            return None
        if record is None:
            logger.info(
                "No '%s' PAT is registered; controllers resolve credentials per repository",
                alias,
            )
            return None
        if not record.is_active:
            logger.warning(
                "The '%s' PAT is deactivated; controllers resolve credentials per repository",
                alias,
            )
            return None
        try:
            credential = await get_provider(AMBIENT_KIND).resolve(record.id or 0)
        except Exception as exc:  # noqa: BLE001 - an unusable PAT is a degraded start
            logger.warning(
                "The '%s' PAT cannot be used (%s); controllers resolve credentials per repository",
                alias,
                exc,
            )
            return None
        logger.info(
            "Controllers use the ambient '%s' PAT (identity=%s)", alias, credential.identity
        )
        return credential

    @staticmethod
    def _binding_resolver():
        """``repo_full_name -> credential`` through ``ct_repository_bindings``.

        A capability asks this before a GitHub call when the process has no
        ambient PAT. A repository that is not subscribed, or has no binding,
        answers ``None`` -- anonymous, which a public repository supports. A
        binding whose credential is dead raises: that must surface rather than
        degrade to an anonymous read that would report a 404 for a private
        repository.
        """
        data = RepositoryData()

        async def resolve(repo_full_name: str):
            repo = await data.by_name(repo_full_name)
            if repo is None:
                return None
            return await resolve_for_repository(repo)

        return resolve

    # -- runtime ---------------------------------------------------------------

    @property
    def log_scope(self) -> Any:
        """How background work gets a connection of its own."""
        return self.database.connection if self.database is not None else None


__all__ = ["AMBIENT_KIND", "Container"]
