# tests/models/test_actions_probe.py
"""Detecting Actions write permission, safely.

Two facts drive this file:

1. GitHub exposes no permission list for fine-grained tokens, and the
   ``permissions`` block of ``/repos/{owner}/{repo}`` has no ``actions`` key --
   it describes repository roles, not token scopes. Reading it cannot answer the
   question.
2. The only reliable answer comes from asking GitHub to accept a dispatch. A
   dispatch to a ref that cannot exist is rejected with 422 *after* the
   permission check, so it proves the permission without starting a run.

The old probe never set ``can_write_actions`` at all, which read as "no write
permission" for every credential -- a silent false negative that disabled
triggering everywhere. These tests pin the three outcomes that matter.

The project's tests drive coroutines through ``asyncio.run`` (see
``conftest.with_db``) rather than ``pytest.mark.asyncio``, so these do the same.
"""

from __future__ import annotations

import asyncio

from conftest import FakeGithub

from rhosocial.github_ci_tracker.core.exceptions import (
    GithubAPIError,
    TokenPermissionError,
    TriggerFailedError,
)
from rhosocial.github_ci_tracker.credentials import AsyncPatCredential, get_provider
from rhosocial.github_ci_tracker.services.github.client import PROBE_REF, AsyncGithubClient

REPO = "acme/demo"


class _Dispatch:
    """Stand-in for the client's HTTP call, returning a chosen outcome."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.calls: list[dict] = []

    async def __call__(self, method, url, token, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.exc is not None:
            raise self.exc
        return object()


def _client(dispatch: _Dispatch, workflows: int = 1) -> AsyncGithubClient:
    """An ``AsyncGithubClient`` whose dispatch and workflow list are stubbed."""
    client = AsyncGithubClient()

    async def fake_request(method, url, token, **kwargs):
        return await dispatch(method, url, token, **kwargs)

    async def fake_workflows(token, repo_full_name):
        return [{"id": 1, "path": ".github/workflows/ci.yml", "state": "active"}][:workflows]

    client._request = fake_request  # type: ignore[method-assign]
    client.get_repo_workflows = fake_workflows  # type: ignore[method-assign]
    return client


def _probe(dispatch: _Dispatch, workflows: int = 1) -> bool:
    """Run one probe against a stubbed client."""

    async def scenario() -> bool:
        client = _client(dispatch, workflows)
        try:
            return await client.probe_actions_write("ghp_x", REPO)
        finally:
            await client.close()

    return asyncio.run(scenario())


def test_probe_reports_true_when_github_rejects_the_ref() -> None:
    """422 on a nonexistent ref proves the permission check passed."""
    dispatch = _Dispatch(TriggerFailedError("No ref found", 422))
    assert _probe(dispatch) is True
    # The probe dispatched to the sentinel ref, which cannot start a run.
    assert dispatch.calls[0]["json"] == {"ref": PROBE_REF}


def test_probe_reports_false_when_permission_is_denied() -> None:
    """403 is GitHub refusing the permission itself."""
    dispatch = _Dispatch(TokenPermissionError("forbidden", 403))
    assert _probe(dispatch) is False


def test_probe_reports_false_for_an_unexpected_refusal() -> None:
    """404 or a transport error is "cannot prove it", never a guess of yes."""
    dispatch = _Dispatch(GithubAPIError("not found", 404))
    assert _probe(dispatch) is False


def test_probe_reports_false_when_no_workflow_exists() -> None:
    """Without a dispatchable workflow there is nothing to ask about."""
    dispatch = _Dispatch()
    assert _probe(dispatch, workflows=0) is False
    assert dispatch.calls == []


def test_fake_client_reports_the_write_capability_shape() -> None:
    """The key must always be present; a missing key reads as "denied"."""
    fake = FakeGithub()

    async def scenario() -> dict:
        return await fake.verify_token("ghp_x")

    raw = asyncio.run(scenario())
    assert "can_write_actions" in raw


async def test_capability_is_persisted_and_reported(bound) -> None:
    """A probed write permission survives the round trip to the database."""
    from datetime import datetime, timezone

    record = AsyncPatCredential(
        name="probe",
        token="ghp_x",
        owner_login="alice",
        is_active=True,
        can_read_actions=True,
        can_write_actions=True,
        # A cached probe result is what ``capabilities`` returns without a
        # refresh; without this timestamp it would go to the network.
        capabilities_probed_at=datetime.now(timezone.utc),
    )
    await record.save()

    stored = await AsyncPatCredential.find_by_name("probe")
    assert stored is not None
    caps = await get_provider("pat").capabilities(stored.id or 0)
    assert caps.can_write_actions is True
    assert caps.identity == "alice"
