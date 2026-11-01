# tests/core/test_error_mapping.py
"""A 403 is not always a permission problem.

GitHub answers 403 for "the token may not do this" *and* for "the run is still
going". Routing the second through the first sends the caller to fix a token
that is fine, so the concurrency case is its own type.
"""

from __future__ import annotations

from rhosocial.github_ci_tracker.core.exceptions import (
    GithubAPIError,
    RateLimitError,
    RunInProgressError,
    TokenPermissionError,
    map_api_error,
)


def _error(status: int, message: str) -> GithubAPIError:
    return GithubAPIError(message, status_code=status)


class TestRunInProgress:
    def test_rerunning_a_running_run_is_a_concurrency_error(self) -> None:
        mapped = map_api_error(_error(403, "This workflow is already running"))

        assert isinstance(mapped, RunInProgressError)
        assert not isinstance(mapped, TokenPermissionError)
        assert mapped.status_code == 403

    def test_a_conflict_status_is_recognised_too(self) -> None:
        mapped = map_api_error(_error(409, "Workflow run is already in progress"))

        assert isinstance(mapped, RunInProgressError)

    def test_the_message_is_kept_verbatim(self) -> None:
        """The caller may quote GitHub's own words back to an operator."""
        mapped = map_api_error(_error(403, "This workflow is already running"))

        assert "already running" in str(mapped)


class TestPermissionStillMaps:
    def test_an_actionable_403_is_a_credential_problem(self) -> None:
        mapped = map_api_error(_error(403, "Resource not accessible by integration"))

        assert isinstance(mapped, TokenPermissionError)

    def test_a_rate_limited_403_is_neither(self) -> None:
        mapped = map_api_error(_error(403, "API rate limit exceeded"))

        assert isinstance(mapped, RateLimitError)

    def test_an_unrelated_error_passes_through(self) -> None:
        original = _error(500, "boom")

        assert map_api_error(original) is original
