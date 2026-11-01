# tests/services/github/test_error_translation.py
"""The transport is where a 403 is decided, so it is where this must be tested.

``map_api_error`` already knew that "This workflow is already running" is not a
permission problem, but it never saw the error: the HTTP client raises
``TokenPermissionError`` for every 403 that is not a rate limit, and an
already-typed error passes through the mapper unchanged. Rerunning a live run
therefore told the caller to re-authorize the token.

These tests pin the classification at the seam that makes it.
"""

from __future__ import annotations

from typing import Any

import httpx2
import pytest

from rhosocial.github_ci_tracker.core.exceptions import (
    RateLimitError,
    RunInProgressError,
    TokenPermissionError,
)
from rhosocial.github_ci_tracker.services.github.client import handle_error


def _response(status: int, message: str, *, headers: dict[str, str] | None = None) -> Any:
    return httpx2.Response(status, json={"message": message}, headers=headers or {})


def test_a_running_run_is_a_state_conflict_not_a_permission_problem() -> None:
    with pytest.raises(RunInProgressError) as caught:
        handle_error(_response(403, "This workflow is already running"))

    assert not isinstance(caught.value, TokenPermissionError)
    assert caught.value.status_code == 403
    assert "already running" in str(caught.value)


@pytest.mark.parametrize(
    "message",
    ["This workflow is already running", "Workflow run is still running"],
)
def test_the_phrases_github_uses_are_recognised(message: str) -> None:
    with pytest.raises(RunInProgressError):
        handle_error(_response(403, message))


def test_an_actionable_403_is_still_a_credential_problem() -> None:
    with pytest.raises(TokenPermissionError):
        handle_error(_response(403, "Resource not accessible by integration"))


def test_a_rate_limited_403_is_still_rate_limited() -> None:
    with pytest.raises(RateLimitError):
        handle_error(_response(403, "API rate limit exceeded", headers={"X-RateLimit-Reset": "0"}))
