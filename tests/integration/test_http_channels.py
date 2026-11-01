# tests/integration/test_http_channels.py
"""Protocol-channel checks against the real server binary and PostgreSQL.

The adapter tests in ``tests/adapters`` exercise the FastAPI app in-process with
the shared unit suite runs directly against PostgreSQL. This module instead boots
the actual server (``serve all``) as a
subprocess against a real PostgreSQL database and talks to it over HTTP, which
is the only way to cover what an in-process app cannot: the lifespan connection
pool, the per-request binding middleware, the CLI's bind defaults, and the
webhook receiver as an HTTP endpoint (signature verification, event routing,
state transitions persisted through the pool).

Nothing here touches GitHub: the trigger cases stop at the policy refusal, and
the webhook cases deliver synthetic signed payloads for a run seeded in the
database. The coverage is therefore deterministic and network-free -- it is the
protocol edges the manual walks found defects in, pinned by CI.

Gated on ``CITRACKER_E2E_POSTGRES=1`` (with ``CITRACKER_E2E_DB_*`` pointing at
the server), so the default suite keeps running without one.
The GitHub Actions ``channels`` job sets it with a PostgreSQL service container.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from rhosocial.activerecord.backend.impl.postgres.backend import AsyncPostgresBackend
from rhosocial.activerecord.backend.impl.postgres.config import PostgresConnectionConfig

from rhosocial.github_ci_tracker.config import DatabaseSettings, Settings
from rhosocial.github_ci_tracker.credentials import AsyncPatCredential
from rhosocial.github_ci_tracker.database import MODELS, Database
from rhosocial.github_ci_tracker.models import (
    AsyncRepository,
    AsyncRepositoryBinding,
    AsyncWorkflow,
    AsyncWorkflowRun,
)
from rhosocial.github_ci_tracker.schema import create_all

#: The gate: unset in the default suite, ``1`` in the CI channels job.
REQUIRES_POSTGRES = "CITRACKER_E2E_POSTGRES"

API_KEY = "e2e-api-key"
WEBHOOK_SECRET = "e2e-webhook-secret"
AUTH = {"Authorization": f"Bearer {API_KEY}"}

pytestmark = pytest.mark.skipif(
    not os.getenv(REQUIRES_POSTGRES),
    reason=f"set {REQUIRES_POSTGRES}=1 with CITRACKER_E2E_DB_* to run this module",
)


def _db_settings() -> dict[str, Any]:
    """Read the E2E database coordinates, defaulting to the project ones."""
    return {
        "host": os.getenv("CITRACKER_E2E_DB_HOST", "192.168.1.3"),
        "port": int(os.getenv("CITRACKER_E2E_DB_PORT", "16689")),
        "username": os.getenv("CITRACKER_E2E_DB_USER", "root"),
        "password": os.getenv("CITRACKER_E2E_DB_PASSWORD", "password"),
        "database": os.getenv("CITRACKER_E2E_DB_NAME", "test_db"),
    }


def _free_port() -> int:
    """Ask the OS for an unused loopback port (small race, accepted)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(coro: Any) -> Any:
    """Run a coroutine on a loop psycopg accepts.

    On Windows psycopg cannot drive sockets on the ProactorEventLoop, so the
    loop is created through ``asyncio.Runner`` -- the same accommodation the
    service's own CLI makes.
    """
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            return runner.run(coro)
    return asyncio.run(coro)


def _sign(payload: bytes, secret: str) -> str:
    """The header value GitHub would send for ``payload``."""
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class _Seed:
    """Everything the seeded repository contributes, by value not by id."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.repo_full_name = f"e2e/{tag}"
        self.repo_github_id = int(tag[:6], 16)
        self.workflow_github_id = int(tag[2:8], 16)
        self.run_github_id = int(tag[6:12], 16) or 1
        self.workflow_path = ".github/workflows/ci.yml"


class _Server:
    """The booted server plus the seeded fixture names.

    ``serve all`` listens on two ports: the REST surface and the webhook
    receiver. Both are recorded because the deliveries must reach the latter.
    """

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        rest_port: int,
        webhook_port: int,
        seed: _Seed,
    ) -> None:
        self.process = process
        self.seed = seed
        self.base = f"http://127.0.0.1:{rest_port}"
        self.webhook_base = f"http://127.0.0.1:{webhook_port}"


def _seed_database(db: dict[str, Any]) -> _Seed:
    """Create the schema and one fully wired repository, synchronously."""
    seed = _Seed(uuid.uuid4().hex)

    async def scenario() -> _Seed:
        backend = AsyncPostgresBackend(connection_config=PostgresConnectionConfig(**db))
        await backend.connect()
        # A bare connection is not dialect-adapted; the service's pool does this
        # on acquire (``AsyncBackendPool``), so an in-process writer must too.
        await backend.introspect_and_adapt()
        previous = [(model, model.__backend__) for model in MODELS]
        for model in MODELS:
            model.__backend__ = backend
        try:
            await create_all(backend)

            credential = AsyncPatCredential(
                name=f"e2e-{seed.tag}",
                # Every column with a UNIQUE constraint gets the tag: a shared
                # database must never see two runs collide.
                token=f"ghp_e2e_{seed.tag}",
                is_active=True,
                can_write_actions=True,
                capabilities_probed_at=datetime(2026, 10, 25, tzinfo=timezone.utc),
            )
            await credential.save()
            repo = AsyncRepository(
                github_repo_id=seed.repo_github_id,
                full_name=seed.repo_full_name,
                owner="e2e",
                name=seed.tag,
                is_private=False,
                default_branch="main",
            )
            await repo.save()
            await AsyncRepositoryBinding.bind(repo.id or 0, "pat", credential.id or 0)
            workflow = AsyncWorkflow(
                repository_id=repo.id or 0,
                workflow_id=seed.workflow_github_id,
                name="CI",
                path=seed.workflow_path,
                state="active",
            )
            await workflow.save()
            run = AsyncWorkflowRun(
                workflow_id=workflow.id or 0,
                github_run_id=seed.run_github_id,
                run_number=7,
                run_attempt=1,
                event="workflow_dispatch",
                status="queued",
                branch="main",
                commit_sha=f"e2e{seed.tag}",
                commit_message="e2e seeded run",
                actor="e2e-bot",
                started_at=datetime(2026, 10, 25, tzinfo=timezone.utc),
            )
            await run.save()
            return seed
        finally:
            for model, prior in previous:
                model.__backend__ = prior
            await backend.disconnect()

    return _run(scenario())


def _cleanup_database(db: dict[str, Any], seed: _Seed) -> None:
    """Remove the seeded rows, so repeated local runs share a database.

    The statements interpolate literals rather than parameters: every value is
    generated here (a hex tag, the derived integer ids, the ``e2e/`` names), so
    there is no untrusted input, and the parameter-preparation path of the
    Postgres dialect is one the ORM's own insert layer never exercises with
    named placeholders.
    """

    async def scenario() -> None:
        backend = AsyncPostgresBackend(connection_config=PostgresConnectionConfig(**db))
        await backend.connect()
        await backend.introspect_and_adapt()
        try:
            # Children first: a run row blocks its workflow, the workflow its
            # repository. A job hangs off an *attempt*, not off the run, so the
            # hop between the jobs and the run is ct_run_attempts.
            jobs_of_run = (
                f"SELECT id FROM ct_jobs WHERE attempt_id IN (SELECT id FROM"
                f" ct_run_attempts WHERE run_id IN (SELECT id FROM ct_workflow_runs"
                f" WHERE github_run_id = {seed.run_github_id}))"
            )
            for statement in (
                "DELETE FROM ct_step_logs WHERE step_id IN"
                f" (SELECT id FROM ct_steps WHERE job_id IN ({jobs_of_run}))",
                f"DELETE FROM ct_steps WHERE job_id IN ({jobs_of_run})",
                f"DELETE FROM ct_job_logs WHERE job_id IN ({jobs_of_run})",
                f"DELETE FROM ct_jobs WHERE attempt_id IN (SELECT id FROM ct_run_attempts"
                f" WHERE run_id IN (SELECT id FROM ct_workflow_runs WHERE"
                f" github_run_id = {seed.run_github_id}))",
                f"DELETE FROM ct_run_attempts WHERE run_id IN (SELECT id FROM"
                f" ct_workflow_runs WHERE github_run_id = {seed.run_github_id})",
                f"DELETE FROM ct_workflow_runs WHERE github_run_id = {seed.run_github_id}",
                f"DELETE FROM ct_workflows WHERE repository_id IN (SELECT id FROM ct_repositories"
                f" WHERE full_name = '{seed.repo_full_name}')",
                f"DELETE FROM ct_repository_bindings WHERE repository_id IN (SELECT id FROM"
                f" ct_repositories WHERE full_name = '{seed.repo_full_name}')",
                f"DELETE FROM ct_repositories WHERE full_name = '{seed.repo_full_name}'",
                f"DELETE FROM ct_pat_credentials WHERE name = 'e2e-{seed.tag}'",
            ):
                await backend.execute(statement)
        finally:
            await backend.disconnect()

    _run(scenario())


def _boot_server(db: dict[str, Any], seed: _Seed) -> _Server:
    """Start ``serve all`` against the E2E database on free loopback ports."""
    rest_port = _free_port()
    webhook_port = _free_port()
    env = dict(os.environ)
    env.update(
        {
            "CITRACKER_DB_HOST": str(db["host"]),
            "CITRACKER_DB_PORT": str(db["port"]),
            "CITRACKER_DB_USER": db["username"],
            "CITRACKER_DB_PASSWORD": db["password"],
            "CITRACKER_DB_NAME": db["database"],
            "CITRACKER_API_KEY": API_KEY,
            "CITRACKER_WEBHOOK_SECRET": WEBHOOK_SECRET,
            "CITRACKER_REST_HOST": "127.0.0.1",
            "CITRACKER_REST_PORT": str(rest_port),
            "CITRACKER_WEBHOOK_HOST": "127.0.0.1",
            "CITRACKER_WEBHOOK_PORT": str(webhook_port),
            "CITRACKER_LOG_LEVEL": "INFO",
            # The module talks plain HTTP. A developer's ``.env`` may configure
            # TLS for the webhook listener (the service reads it on startup), so
            # the certificate is explicitly cleared here: ``_load_dotenv`` never
            # overrides a variable that is already set, empty included.
            "CITRACKER_WEBHOOK_SSL_CERTFILE": "",
            "CITRACKER_WEBHOOK_SSL_KEYFILE": "",
        }
    )
    # The suite is network-free; a proxy in the developer's environment must not
    # reach the subprocess.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)

    project_root = Path(__file__).resolve().parents[2]
    process = subprocess.Popen(
        [sys.executable, "-m", "rhosocial.github_ci_tracker.adapters.cli.main", "serve", "all"],
        cwd=project_root,
        env=env,
    )
    return _Server(process, rest_port, webhook_port, seed)


def _wait_until_ready(server: _Server, timeout: float = 45.0) -> None:
    """Block until the REST listener answers, or fail with the exit status."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.process.poll() is not None:
            pytest.fail(f"server exited early with code {server.process.returncode}")
        try:
            response = httpx.get(f"{server.base}/healthz", timeout=2.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    pytest.fail("server did not become ready in time")


@pytest.fixture(scope="module")
def server() -> Iterator[_Server]:
    """One booted server, one seeded repository, for the whole module."""
    db = _db_settings()
    seed = _seed_database(db)
    booted = _boot_server(db, seed)
    try:
        _wait_until_ready(booted)
        yield booted
    finally:
        booted.process.terminate()
        try:
            booted.process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            booted.process.kill()
        _cleanup_database(db, seed)


def _workflow_run_payload(seed: _Seed, action: str, status: str, conclusion: str | None) -> bytes:
    """A ``workflow_run`` delivery body in GitHub's shape, JSON-encoded."""
    return json.dumps(
        {
            "action": action,
            "workflow_run": {
                "id": seed.run_github_id,
                "run_number": 7,
                "run_attempt": 1,
                "event": "workflow_dispatch",
                "status": status,
                "conclusion": conclusion,
                "head_branch": "main",
                "head_sha": f"e2e{seed.tag}",
                "head_commit": {"message": "e2e seeded run"},
                "actor": {"login": "e2e-bot"},
                "run_started_at": "2026-10-25T00:00:00Z",
                "workflow_id": seed.workflow_github_id,
                "path": seed.workflow_path,
                "name": "CI",
            },
            "repository": {
                "id": seed.repo_github_id,
                "full_name": seed.repo_full_name,
                "owner": {"login": "e2e"},
                "name": seed.tag,
                "private": False,
                "default_branch": "main",
            },
        }
    ).encode("utf-8")


def _deliver(server: _Server, payload: bytes, signature: str | None) -> httpx.Response:
    """POST one webhook delivery, forging the signature when asked to."""
    headers = {"X-GitHub-Event": "workflow_run"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return httpx.post(f"{server.webhook_base}/webhooks/github", content=payload, headers=headers)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def _stored_run(seed: _Seed) -> AsyncWorkflowRun | None:
    """Read the seeded run's row, newest attempt's state included.

    Opens its own connection: the server under test owns the pool, and this is
    the observation point the tests need when the read surfaces deliberately go
    to GitHub.
    """
    db = _db_settings()

    async def read() -> AsyncWorkflowRun | None:
        # A query resolves its backend from the pool's context variable, so the
        # read goes through a pool exactly like the server's own reads do.
        database = await Database.create(Settings(database=DatabaseSettings(**db)))
        try:
            async with database.connection():
                return await AsyncWorkflowRun.find_by_github_id(seed.run_github_id, derived=True)
        finally:
            await database.close()

    return _run(read())


def test_readyz_needs_no_credentials(server: _Server) -> None:
    """Readiness is what the deployment probes; it must not demand an API key."""
    response = httpx.get(f"{server.base}/readyz", timeout=10.0)

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["checks"]["database"] == "ok"


def test_api_requires_a_bearer_token(server: _Server) -> None:
    response = httpx.get(
        f"{server.base}/api/v1/workflows", params={"repository": server.seed.repo_full_name}
    )

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_api_rejects_a_wrong_key(server: _Server) -> None:
    response = httpx.get(
        f"{server.base}/api/v1/workflows",
        params={"repository": server.seed.repo_full_name},
        headers={"Authorization": "Bearer not-the-key"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"


def test_seeded_workflow_is_listable(server: _Server) -> None:
    """The per-request database binding works over the real pool."""
    response = httpx.get(
        f"{server.base}/api/v1/workflows",
        params={"repository": server.seed.repo_full_name},
        headers=AUTH,
    )

    assert response.status_code == 200
    paths = [item["path"] for item in response.json()]
    assert server.seed.workflow_path in paths


# ---------------------------------------------------------------------------
# Trigger: the policy surface over real HTTP
# ---------------------------------------------------------------------------


def test_trigger_with_unknown_workflow_is_not_found(server: _Server) -> None:
    response = httpx.post(
        f"{server.base}/api/v1/workflows/999999999/trigger",
        json={"repository": server.seed.repo_full_name, "ref": "main"},
        headers=AUTH,
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_trigger_with_unsubscribed_repository_is_not_found(server: _Server) -> None:
    response = httpx.post(
        f"{server.base}/api/v1/workflows/{server.seed.workflow_github_id}/trigger",
        json={"repository": "e2e/not-subscribed", "ref": "main"},
        headers=AUTH,
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


# ---------------------------------------------------------------------------
# Webhook: signature, routing, and the run lifecycle persisted through the pool
# ---------------------------------------------------------------------------


def test_webhook_rejects_a_forged_signature(server: _Server) -> None:
    payload = _workflow_run_payload(server.seed, "in_progress", "in_progress", None)

    response = _deliver(server, payload, "sha256=deadbeef")

    assert response.status_code == 401


def test_webhook_accepts_a_signed_delivery_and_persists_the_transition(
    server: _Server,
) -> None:
    """queued -> in_progress -> completed/success, all through the push channel."""
    seed = server.seed

    started = _workflow_run_payload(seed, "in_progress", "in_progress", None)
    response = _deliver(server, started, _sign(started, WEBHOOK_SECRET))
    assert response.status_code == 200
    assert response.json()["handled"] is True

    # The state is read straight from the database. No HTTP surface can answer
    # this anymore: a run-level read goes to GitHub on purpose, and this
    # repository exists nowhere but here -- which is exactly what the push
    # channel is being tested for.
    stored = _stored_run(seed)
    assert stored is not None
    assert (stored.latest_status, stored.latest_conclusion) == ("in_progress", None)

    finished = _workflow_run_payload(seed, "completed", "completed", "success")
    response = _deliver(server, finished, _sign(finished, WEBHOOK_SECRET))
    assert response.status_code == 200
    assert response.json()["handled"] is True

    stored = _stored_run(seed)
    assert stored is not None
    assert (stored.latest_status, stored.latest_conclusion) == ("completed", "success")
    # The run keeps its identity through the transitions.
    assert stored.run_number == 7
    assert stored.branch == "main"
