# tests/integration/test_webhook_burst.py
"""A burst of job deliveries: all of them must be stored, none may fail.

GitHub sends one ``workflow_job`` delivery per job, so a matrix run produces a
dozen within a second or two. The ledger recorded a few of those as
``TransactionError: No active transaction to rollback`` -- the write was lost
until a later delivery or a read repaired it. This module reproduces the burst
against a real server and PostgreSQL.

Gated on ``CITRACKER_E2E_POSTGRES`` like ``test_http_channels``.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import test_http_channels as e2e

pytestmark = pytest.mark.skipif(
    not os.getenv(e2e.REQUIRES_POSTGRES),
    reason=f"set {e2e.REQUIRES_POSTGRES}=1 with CITRACKER_E2E_DB_* to run this module",
)

JOBS = 12


@pytest.fixture(scope="module")
def server() -> Iterator[e2e._Server]:
    """One booted server, one seeded repository, for the whole module."""
    db = e2e._db_settings()
    seed = e2e._seed_database(db)
    booted = e2e._boot_server(db, seed)
    try:
        e2e._wait_until_ready(booted)
        yield booted
    finally:
        booted.process.terminate()
        try:
            booted.process.wait(timeout=15)
        except Exception:  # pragma: no cover - defensive
            booted.process.kill()
        e2e._cleanup_database(db, seed)


def _repository(seed: e2e._Seed) -> dict[str, object]:
    return {
        "id": seed.repo_github_id,
        "full_name": seed.repo_full_name,
        "owner": {"login": "e2e"},
        "name": seed.tag,
        "private": False,
        "default_branch": "main",
    }


def _job_payload(seed: e2e._Seed, job_id: int, index: int) -> bytes:
    return json.dumps(
        {
            "action": "completed",
            "workflow_job": {
                "id": job_id,
                "run_id": seed.run_github_id,
                "run_attempt": 1,
                "name": f"job-{index}",
                "status": "completed",
                "conclusion": "success",
                "started_at": "2026-10-25T00:00:10Z",
                "completed_at": "2026-10-25T00:05:00Z",
                "runner_name": "ubuntu-latest",
                "steps": [
                    {
                        "number": 1,
                        "name": "run",
                        "status": "completed",
                        "conclusion": "success",
                        "started_at": "2026-10-25T00:00:10Z",
                        "completed_at": "2026-10-25T00:05:00Z",
                    }
                ],
            },
            "repository": _repository(seed),
        }
    ).encode("utf-8")


def _post(server: e2e._Server, payload: bytes, delivery_id: str) -> httpx.Response:
    return httpx.post(
        f"{server.webhook_base}/webhooks/github",
        content=payload,
        headers={
            "X-GitHub-Event": "workflow_job",
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": e2e._sign(payload, e2e.WEBHOOK_SECRET),
        },
        timeout=30.0,
    )


def test_a_burst_of_job_deliveries_is_fully_stored(server: e2e._Server) -> None:
    seed = server.seed

    # The run has to exist: job events do not carry the run's fields.
    run_payload = e2e._workflow_run_payload(seed, "completed", "completed", "success")
    created = httpx.post(
        f"{server.webhook_base}/webhooks/github",
        content=run_payload,
        headers={
            "X-GitHub-Event": "workflow_run",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": e2e._sign(run_payload, e2e.WEBHOOK_SECRET),
        },
        timeout=30.0,
    )
    assert created.status_code == 200

    base_job_id = 8_000_000 + (seed.run_github_id % 1_000_000) * 20
    payloads = [(_job_payload(seed, base_job_id + i, i), str(uuid.uuid4())) for i in range(JOBS)]

    with ThreadPoolExecutor(max_workers=JOBS) as pool:
        responses = list(pool.map(lambda item: _post(server, *item), payloads))

    assert [response.status_code for response in responses] == [200] * JOBS
    rejected = [response.json() for response in responses if not response.json()["handled"]]
    assert rejected == []
