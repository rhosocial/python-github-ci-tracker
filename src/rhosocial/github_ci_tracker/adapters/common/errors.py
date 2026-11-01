# src/rhosocial/github_ci_tracker/adapters/common/errors.py
"""Error type definitions for CI Tracker."""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """Error code enumeration."""

    # Auth errors (401, 403)
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"

    # Client errors (400, 404, 409, 422)
    VALIDATION_ERROR = "validation_error"
    NOT_FOUND = "not_found"
    DUPLICATE_RESOURCE = "duplicate_resource"
    CONFLICT = "conflict"

    # GitHub API errors
    GITHUB_AUTH_ERROR = "github_auth_error"
    GITHUB_PERMISSION_ERROR = "github_permission_error"
    GITHUB_NOT_FOUND = "github_not_found"
    GITHUB_RATE_LIMIT = "github_rate_limit"
    GITHUB_SERVER_ERROR = "github_server_error"
    #: GitHub could not be reached; nothing about the request is wrong.
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"

    # Server errors (500)
    INTERNAL_ERROR = "internal_error"
    SERVICE_UNAVAILABLE = "service_unavailable"


class CitrackerError(Exception):
    """Base exception for CI Tracker."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        status_code: int = 500,
        details: dict | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        super().__init__(message)


class PermissionDeniedError(CitrackerError):
    """Permission denied error."""

    def __init__(self, message: str = "Permission denied") -> None:
        super().__init__(ErrorCode.FORBIDDEN, message, 403)


class NotFoundError(CitrackerError):
    """Resource not found error."""

    def __init__(self, resource: str, identifier: str) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            f"{resource} '{identifier}' not found",
            404,
        )


class ValidationError(CitrackerError):
    """Validation error."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(ErrorCode.VALIDATION_ERROR, message, 422, details)


class DuplicateResourceError(CitrackerError):
    """Duplicate resource error."""

    def __init__(self, resource: str, identifier: str) -> None:
        super().__init__(
            ErrorCode.DUPLICATE_RESOURCE,
            f"{resource} '{identifier}' already exists",
            409,
        )


class ConflictError(CitrackerError):
    """Request conflicts with the current resource state (HTTP 409)."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(ErrorCode.CONFLICT, message, 409, details)


class GitHubAPIError(CitrackerError):
    """GitHub API error."""

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        github_error_code: str | None = None,
    ) -> None:
        code = {
            401: ErrorCode.GITHUB_AUTH_ERROR,
            403: ErrorCode.GITHUB_PERMISSION_ERROR,
            404: ErrorCode.GITHUB_NOT_FOUND,
            422: ErrorCode.VALIDATION_ERROR,
        }.get(status_code, ErrorCode.GITHUB_SERVER_ERROR)
        super().__init__(code, message, status_code, {"github_code": github_error_code})


class InternalError(CitrackerError):
    """Internal server error."""

    def __init__(self, message: str = "Internal server error") -> None:
        super().__init__(ErrorCode.INTERNAL_ERROR, message, 500)


def from_core_exception(exc: Exception) -> CitrackerError:
    """Translate a business-core exception into the HTTP error vocabulary.

    The core raises its own exception hierarchy, which knows nothing about
    HTTP; the adapter classes here know nothing about webhooks or GitHub.
    Without this translation an unsubscribed repository or an unknown
    workflow surfaces as an opaque 500 at the protocol edge, when both are
    simply bad requests the caller can correct.

    The mapping follows the same semantics the MCP adapter uses, so the three
    protocols answer the same failure with the same meaning: a missing resource
    is 404, a credential problem is 403 until an operator fixes the deployment,
    and an upstream GitHub rejection keeps GitHub's own status.

    Args:
        exc: The exception raised by the core.

    Returns:
        The adapter exception carrying the HTTP status and error code.
    """
    from ...core import exceptions as core

    if isinstance(
        exc,
        (
            core.RepositoryNotFoundError,
            core.WorkflowNotFoundError,
            core.AttemptNotFoundError,
            core.RunNotFoundError,
            core.JobNotFoundError,
            core.StepNotFoundError,
            core.WebhookNotFoundError,
        ),
    ):
        return CitrackerError(ErrorCode.NOT_FOUND, str(exc), 404)

    if isinstance(exc, core.RunInProgressError):
        # 409: the request conflicts with the run's current state. The caller can
        # repeat it once the run finishes, which is not what 403 would imply.
        return ConflictError(str(exc))

    if isinstance(
        exc,
        (
            core.TokenInUseError,
            core.TokenAlreadyExistsError,
            core.ConcurrentUpdateError,
        ),
    ):
        return CitrackerError(ErrorCode.CONFLICT, str(exc), 409)

    if isinstance(
        exc,
        (
            core.TokenNotFoundError,
            core.TokenRequiredError,
            core.TokenDeactivatedError,
            core.TokenExpiredError,
            core.TokenInvalidError,
            core.TokenPermissionError,
        ),
    ):
        return PermissionDeniedError(str(exc))

    if isinstance(exc, core.ValidationError):
        return CitrackerError(ErrorCode.VALIDATION_ERROR, str(exc), 422)

    if isinstance(exc, core.RateLimitError):
        return CitrackerError(ErrorCode.GITHUB_RATE_LIMIT, str(exc), 429)

    if isinstance(exc, core.UpstreamUnavailableError):
        # 503, not 502: nothing about the request is wrong and the service is
        # fine; the route to GitHub is. ``Retry-After`` is the caller's call.
        return CitrackerError(ErrorCode.UPSTREAM_UNAVAILABLE, str(exc), 503)

    if isinstance(exc, core.InvalidStateTransitionError):
        return CitrackerError(ErrorCode.CONFLICT, str(exc), 409)

    if isinstance(exc, (core.TriggerFailedError, core.GithubAPIError)):
        # GitHub rejected the dispatch, or the created run could not be
        # resolved. Keep GitHub's status when there is one; a missing one
        # means "we could not tell", which is an upstream failure.
        status = getattr(exc, "status_code", 0) or 502
        return GitHubAPIError(str(exc), status)

    # Unknown failure: never leak the exception text to the caller.
    return InternalError()
