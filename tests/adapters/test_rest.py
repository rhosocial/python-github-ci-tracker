# tests/adapters/test_rest.py
"""REST adapter tests: the latest-run route, the trigger route and error mapping.

Only the adapter layer is exercised -- the business core behind the routes is
covered by the model tests, so these cases assert HTTP status codes, the
serialization shape and the API-key middleware contract, not business rules.
GitHub is replaced by :class:`~conftest.FakeGithub` through a FastAPI
dependency override; the database is the session-shared PostgreSQL schema,
bound inside a test lifespan instead of the production PostgreSQL pool.

The trigger cases pin the core-exception translation: a policy refusal is 403
with the reason code, an unknown repository or workflow is 404, and neither
may leak as a 500.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from conftest import FakeGithub, bound_schema
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rhosocial.github_ci_tracker.adapters.common.errors import from_core_exception
from rhosocial.github_ci_tracker.adapters.rest.app import create_app
from rhosocial.github_ci_tracker.container import Container
from rhosocial.github_ci_tracker.core.exceptions import (
    GithubAPIError,
    RateLimitError,
    RepositoryNotFoundError,
    WorkflowNotFoundError,
)
from rhosocial.github_ci_tracker.credentials import AsyncPatCredential
from rhosocial.github_ci_tracker.models import AsyncRepository, AsyncWorkflow
from rhosocial.github_ci_tracker.models.binding import AsyncRepositoryBinding

API_KEY = "test-api-key"
AUTH = {"Authorization": f"Bearer {API_KEY}"}


@asynccontextmanager
async def _test_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Bind a clean schema and seed one subscription."""
    del app  # the production lifespan uses it for the connection pool
    async with bound_schema():
        credential = AsyncPatCredential(name="laptop", token="ghp_test_token", is_active=True)
        await credential.save()
        repo = AsyncRepository(
            github_repo_id=99,
            full_name="acme/demo",
            owner="acme",
            name="demo",
            is_private=True,
            default_branch="main",
        )
        await repo.save()
        await AsyncRepositoryBinding.bind(repo.id or 0, "pat", credential.id or 0)
        yield


def _build_app(
    monkeypatch: pytest.MonkeyPatch,
    fake_client: FakeGithub,
    *,
    api_key: str | None = API_KEY,
) -> FastAPI:
    """Create the REST app wired to the fake client and the test lifespan."""
    if api_key is None:
        monkeypatch.delenv("CITRACKER_API_KEY", raising=False)
    else:
        monkeypatch.setenv("CITRACKER_API_KEY", api_key)
    app = create_app(Container.build(client=fake_client))
    app.router.lifespan_context = _test_lifespan
    return app


def test_tokens_are_listed_with_utc_timestamps(
    monkeypatch: pytest.MonkeyPatch, fake_client: FakeGithub
) -> None:
    """The list route read a key the provider did not return, and 500'd."""
    app = _build_app(monkeypatch, fake_client)

    with TestClient(app) as client:
        response = client.get("/api/v1/tokens", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert [item["token_name"] for item in body] == ["laptop"]
    assert body[0]["created_at"].endswith("+00:00")


def test_latest_run_without_api_key_is_unauthorized(
    monkeypatch: pytest.MonkeyPatch, fake_client: FakeGithub
) -> None:
    app = _build_app(monkeypatch, fake_client)

    with TestClient(app) as client:
        response = client.get("/api/v1/latest-run", params={"repository": "acme/demo"})

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_empty_api_key_allows_anonymous_access(
    monkeypatch: pytest.MonkeyPatch, fake_client: FakeGithub
) -> None:
    """Empty authorization is the supported local-development mode.

    The request carries no Authorization header at all: comparing against an
    empty key would accept "Bearer " and reject this, which is neither mode.
    """
    app = _build_app(monkeypatch, fake_client, api_key=None)

    with TestClient(app) as client:
        response = client.get("/api/v1/latest-run", params={"repository": "acme/demo"})

    assert response.status_code == 200


def test_latest_run_with_wrong_key_is_forbidden(
    monkeypatch: pytest.MonkeyPatch, fake_client: FakeGithub
) -> None:
    app = _build_app(monkeypatch, fake_client)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/latest-run",
            params={"repository": "acme/demo"},
            headers={"Authorization": "Bearer wrong-key"},
        )

    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"


def test_healthz_is_exempt_from_api_key(
    monkeypatch: pytest.MonkeyPatch, fake_client: FakeGithub
) -> None:
    app = _build_app(monkeypatch, fake_client)

    with TestClient(app) as client:
        response = client.get("/healthz")
        # The same probe under the name an external monitor uses: a 401 on a
        # health path reads as an outage.
        alias = client.get("/health")

    # The container under test has no database seam, so the readiness answer is
    # 503 -- what matters here is that no 401 was produced, and that both names
    # reach the same handler.
    assert response.status_code in (200, 503)
    assert response.status_code != 401
    assert "checks" in response.json()
    assert alias.status_code == response.status_code
    assert alias.json() == response.json()


def test_latest_run_returns_the_newest_run(
    monkeypatch: pytest.MonkeyPatch, fake_client: FakeGithub
) -> None:
    """200 with the run serialized per RunStatusResponse (run_id = GitHub id)."""
    app = _build_app(monkeypatch, fake_client)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/latest-run", params={"repository": "acme/demo"}, headers=AUTH
        )

        assert response.status_code == 200
        body = response.json()
        assert body["run_id"] == 500  # service: run_id is the GitHub run id
        assert body["run_number"] == 1
        assert body["status"] == "completed"
        assert body["conclusion"] == "success"
        assert body["branch"] == "main"
        assert body["commit_sha"] == "abc"
        assert body["actor"] == "alice"
        assert body["started_at"] is not None

        # The adopted run is now part of the local mirror and listable without
        # any further GitHub call (list_runs reads the database only).
        listing = client.get("/api/v1/runs", params={"repository": "acme/demo"}, headers=AUTH)
        assert listing.status_code == 200
        assert [item["run_id"] for item in listing.json()] == [500]


def test_latest_run_without_runs_returns_null(
    monkeypatch: pytest.MonkeyPatch, fake_client: FakeGithub
) -> None:
    app = _build_app(monkeypatch, fake_client)
    fake_client.runs = []

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/latest-run", params={"repository": "acme/demo"}, headers=AUTH
        )

    assert response.status_code == 200
    assert response.json() is None


# ---------------------------------------------------------------------------
# Trigger route: the core-exception translation
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _trigger_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Bind a clean schema, seed one subscription and one workflow row."""
    del app  # the production lifespan uses it for the connection pool
    async with bound_schema():
        # can_write_actions is the probed credential capability; without it the
        # dispatch is refused on the credential gate, which is a different (also
        # correct) refusal than the one under test.
        credential = AsyncPatCredential(
            name="laptop",
            token="ghp_test_token",
            is_active=True,
            can_write_actions=True,
            capabilities_probed_at=datetime(2026, 10, 25, tzinfo=timezone.utc),
        )
        await credential.save()
        repo = AsyncRepository(
            github_repo_id=99,
            full_name="acme/demo",
            owner="acme",
            name="demo",
            is_private=True,
            default_branch="main",
        )
        await repo.save()
        await AsyncRepositoryBinding.bind(repo.id or 0, "pat", credential.id or 0)
        await AsyncWorkflow(
            repository_id=repo.id or 0,
            workflow_id=555,
            name="CI",
            path=".github/workflows/ci.yml",
            state="active",
        ).save()
        yield


def _build_trigger_app(monkeypatch: pytest.MonkeyPatch, fake_client: FakeGithub) -> FastAPI:
    """Create the REST app wired to the fake client and the trigger lifespan."""
    monkeypatch.setenv("CITRACKER_API_KEY", API_KEY)
    # The container, with the transport faked: the route then sees the same
    # controllers a server builds.
    app = create_app(Container.build(client=fake_client))
    app.router.lifespan_context = _trigger_lifespan
    return app


def test_trigger_unknown_workflow_is_not_found(
    monkeypatch: pytest.MonkeyPatch, fake_client: FakeGithub
) -> None:
    """An unknown workflow id is a bad request, not a server error."""
    app = _build_trigger_app(monkeypatch, fake_client)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/workflows/999999/trigger",
            json={"repository": "acme/demo", "ref": "main"},
            headers=AUTH,
        )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_trigger_unsubscribed_repository_is_not_found(
    monkeypatch: pytest.MonkeyPatch, fake_client: FakeGithub
) -> None:
    """Triggering a repository the deployment does not track answers 404."""
    app = _build_trigger_app(monkeypatch, fake_client)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/workflows/555/trigger",
            json={"repository": "acme/missing", "ref": "main"},
            headers=AUTH,
        )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_core_exception_vocabulary() -> None:
    """The translation covers the whole core hierarchy deterministically."""

    missing_repo = from_core_exception(RepositoryNotFoundError("not subscribed"))
    assert (missing_repo.status_code, missing_repo.code.value) == (404, "not_found")

    missing_workflow = from_core_exception(WorkflowNotFoundError("unknown"))
    assert (missing_workflow.status_code, missing_workflow.code.value) == (404, "not_found")

    throttled = from_core_exception(RateLimitError("slow down"))
    assert (throttled.status_code, throttled.code.value) == (429, "github_rate_limit")

    # A missing upstream status means "could not tell": an upstream failure,
    # never the caller's fault.
    upstream = from_core_exception(GithubAPIError("boom"))
    assert upstream.status_code == 502
    assert upstream.code.value == "github_server_error"

    # Anything unrecognized degrades to a bare 500 without leaking the text.
    unknown = from_core_exception(RuntimeError("secret internals"))
    assert (unknown.status_code, unknown.code.value) == (500, "internal_error")
    assert "secret internals" not in unknown.message


def test_the_log_response_carries_the_step_sections() -> None:
    """The REST view must expose the same source/sections the facade returns."""
    from rhosocial.github_ci_tracker.adapters.rest.routes.jobs import _logs_response
    from rhosocial.github_ci_tracker.views import JobLogsView, StepSection

    view = JobLogsView(
        job_id=7,
        job_name="build",
        log_content="boom\n",
        source="step",
        sections=[
            StepSection(
                number=2,
                name="Run tests",
                conclusion="failure",
                content="boom\n",
                line_count=1,
            )
        ],
        steps_without_logs=[3],
        reason=None,
    )

    payload = _logs_response(view)

    assert payload.source == "step"
    assert [section.model_dump() for section in payload.sections] == [
        {
            "step": 2,
            "name": "Run tests",
            "conclusion": "failure",
            "content": "boom\n",
            "lines": 1,
        }
    ]
    assert payload.steps_without_logs == [3]
