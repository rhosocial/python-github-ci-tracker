# tests/services/github/test_upstream_unavailable.py
"""A dropped connection is an upstream problem, and a POST is never retried.

``RemoteProtocolError`` is a ``TransportError`` but not a ``NetworkError``, so
the old ``except (TimeoutException, NetworkError)`` let it escape untyped: the
caller saw ``internal_error: RemoteProtocolError: Server disconnected...`` and
could only conclude that citracker was broken.

The other half is the retry policy. A dispatch that timed out may already have
been accepted by GitHub; retrying it starts a second run. Only idempotent
methods are retried, and the rest fail fast with a typed error.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx2
import pytest

from rhosocial.github_ci_tracker.adapters.mcp.result import error_result
from rhosocial.github_ci_tracker.core.exceptions import UpstreamUnavailableError
from rhosocial.github_ci_tracker.services.github.client import AsyncGithubClient

REPO = "acme/demo"


class _DroppingClient:
    """An HTTP client whose every request loses the connection."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.is_closed = False

    async def request(self, method: str, url: str, **_: Any) -> Any:
        self.calls.append(method)
        raise httpx2.RemoteProtocolError("Server disconnected without sending a response.")

    async def aclose(self) -> None:
        self.is_closed = True


def _client() -> tuple[AsyncGithubClient, _DroppingClient]:
    client = AsyncGithubClient(max_retries=3, retry_delay=0.0)
    dropping = _DroppingClient()
    client._client = dropping  # noqa: SLF001 - the transport seam under test
    return client, dropping


def test_a_dropped_connection_is_typed_and_retried_for_a_get() -> None:
    client, dropping = _client()

    async def body() -> None:
        with pytest.raises(UpstreamUnavailableError) as caught:
            await client.get_run(None, REPO, 1)
        assert "did not answer" in str(caught.value)

    asyncio.run(body())
    assert dropping.calls == ["GET", "GET", "GET"], "an idempotent read is retried"


def test_a_dispatch_is_not_retried() -> None:
    """A dispatch that failed at the transport may have been accepted."""
    client, dropping = _client()

    async def body() -> None:
        with pytest.raises(UpstreamUnavailableError) as caught:
            await client.trigger_workflow("token", REPO, "7", "main")
        assert "not retried" in str(caught.value)

    asyncio.run(body())
    assert dropping.calls == ["POST"], "one attempt, because a retry starts a second run"


def test_the_envelope_tells_the_caller_not_to_change_the_request() -> None:
    payload = error_result(UpstreamUnavailableError("GitHub did not answer"))

    assert payload["error"]["code"] == "upstream_unavailable"
    assert "not change the arguments" in payload["error"]["hint"]


class _RecordingClient:
    """An HTTP client that records the URL and answers an empty page."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.is_closed = False

    async def request(self, method: str, url: str, **_: Any) -> Any:
        self.urls.append(url)
        return httpx2.Response(
            200,
            json={"workflow_runs": [], "total_count": 0},
            request=httpx2.Request(method, "https://api.github.com"),
        )

    async def aclose(self) -> None:
        self.is_closed = True


def test_a_workflow_filter_uses_the_workflow_scoped_endpoint() -> None:
    """``?workflow_id=`` on the repo-wide endpoint is ignored by GitHub.

    Verified against the live API: a filtered call and an unfiltered one
    returned the same page, so ``find_runs(workflow=...)`` looked like it
    worked while answering with every workflow's runs.
    """
    client = AsyncGithubClient(max_retries=1, retry_delay=0.0)
    recording = _RecordingClient()
    client._client = recording  # noqa: SLF001 - the transport seam under test

    async def body() -> None:
        await client.list_runs(None, REPO, workflow_id=7)
        await client.list_runs(None, REPO)

    asyncio.run(body())
    assert recording.urls[0] == "/repos/acme/demo/actions/workflows/7/runs"
    assert recording.urls[1] == "/repos/acme/demo/actions/runs"


def test_the_ref_head_is_read_from_the_commits_endpoint() -> None:
    """A branch name is not a ref path: ``/commits/{ref}`` resolves both."""
    client = AsyncGithubClient(max_retries=1, retry_delay=0.0)
    recording = _RecordingClient()
    client._client = recording  # noqa: SLF001 - the transport seam under test

    async def body() -> str:
        return await client.get_ref_sha(None, REPO, "main")

    # ``_RecordingClient`` answers an empty run page; the sha is simply absent,
    # which is what the URL assertion is here for.
    assert asyncio.run(body()) == ""
    assert recording.urls == ["/repos/acme/demo/commits/main"]
