# tests/services/github/test_conditional_requests.py
"""Every read goes to GitHub; an unchanged answer must not cost quota.

GitHub does not count ``304 Not Modified`` against the rate limit, so the client
sends ``If-None-Match`` and replays the stored body. That is what keeps "read the
source on every query" affordable -- without it the rule would be correct and
unusable.
"""

from __future__ import annotations

import httpx2

from rhosocial.github_ci_tracker.services.github.client import (
    BASE_URL,
    AsyncGithubClient,
    GithubClient,
)

PAYLOAD = {"id": 90_001, "status": "completed", "conclusion": "success"}


def _transport(handler):
    return httpx2.MockTransport(handler)


async def test_a_repeat_read_is_conditional_and_replays_the_body() -> None:
    seen: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("if-none-match"))
        if len(seen) == 1:
            return httpx2.Response(
                200, json=PAYLOAD, headers={"ETag": 'W/"abc"', "Link": '<x>; rel="next"'}
            )
        return httpx2.Response(304, headers={"ETag": 'W/"abc"'})

    client = AsyncGithubClient()
    client._client = httpx2.AsyncClient(
        transport=_transport(handler), base_url=BASE_URL
    )  # noqa: SLF001

    first = await client.get_run(token="ghp_x", repo_full_name="acme/demo", run_id=90_001)
    second = await client.get_run(token="ghp_x", repo_full_name="acme/demo", run_id=90_001)

    assert first == second == PAYLOAD
    assert seen == [None, 'W/"abc"'], "the second call asks conditionally"


def test_the_replayed_answer_keeps_the_pagination_header() -> None:
    """``_paginate`` reads ``Link``; a replay without it would stop early."""
    seen: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("if-none-match"))
        if len(seen) == 1:
            return httpx2.Response(
                200, json=PAYLOAD, headers={"ETag": 'W/"abc"', "Link": '<x>; rel="next"'}
            )
        return httpx2.Response(304)

    client = GithubClient()
    client._client = httpx2.Client(transport=_transport(handler), base_url=BASE_URL)  # noqa: SLF001

    first = client._request("GET", "/repos/acme/demo/actions/runs/1", "ghp_x")  # noqa: SLF001
    second = client._request("GET", "/repos/acme/demo/actions/runs/1", "ghp_x")  # noqa: SLF001

    assert first.json() == PAYLOAD
    assert second.status_code == 200
    assert second.json() == PAYLOAD
    assert second.headers["Link"] == '<x>; rel="next"'


async def test_another_credential_does_not_reuse_the_etag() -> None:
    """The same URL answers differently for another token, so the key includes it."""
    seen: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("if-none-match"))
        return httpx2.Response(200, json=PAYLOAD, headers={"ETag": 'W/"abc"'})

    client = AsyncGithubClient()
    client._client = httpx2.AsyncClient(
        transport=_transport(handler), base_url=BASE_URL
    )  # noqa: SLF001

    await client.get_run(token="ghp_a", repo_full_name="acme/demo", run_id=90_001)
    await client.get_run(token="ghp_b", repo_full_name="acme/demo", run_id=90_001)

    assert seen == [None, None]
