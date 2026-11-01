# src/rhosocial/github_ci_tracker/adapters/rest/routes/tokens.py
"""Token routes.

Responses carry token metadata only -- never the token value (security
invariant). Every operation goes through ``TokenController``; this module binds
parameters, maps errors and serializes the metadata view it is given.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, status

from ....views import TokenRecord
from ...common.errors import (
    CitrackerError,
    ConflictError,
    DuplicateResourceError,
    GitHubAPIError,
    InternalError,
    NotFoundError,
    ValidationError,
)
from ..schemas.requests import AddTokenRequest, RotateTokenRequest
from ..schemas.responses import TokenMetaResponse

router = APIRouter()


def get_token_service(request: Request):
    """Dependency: the credential controller this application serves from."""
    return request.app.state.container.token


def _map_token_error(name: str, exc: Exception) -> CitrackerError:
    """Map a core token error onto its REST counterpart.

    Core error messages describe aliases and counts only, never the token
    value, so forwarding ``str(exc)`` keeps the response safety invariant.

    Args:
        name: Token alias the failing operation referred to.
        exc: The raised core exception.

    Returns:
        The REST error to raise instead.
    """
    from ....core import exceptions as core_errors

    if isinstance(exc, core_errors.TokenNotFoundError):
        return NotFoundError("token", name)
    if isinstance(exc, core_errors.TokenAlreadyExistsError):
        return DuplicateResourceError("token", name)
    if isinstance(exc, core_errors.TokenInUseError):
        return ConflictError(str(exc), details={"token_name": name})
    if isinstance(exc, core_errors.TokenInvalidError):
        return GitHubAPIError(str(exc), 401)
    if isinstance(exc, core_errors.RateLimitError):
        return GitHubAPIError(str(exc), 429)
    if isinstance(exc, core_errors.GithubAPIError):
        return GitHubAPIError(str(exc), exc.status_code)
    if isinstance(exc, core_errors.ValidationError):
        return ValidationError(str(exc))
    return InternalError(f"Token operation for '{name}' failed: {exc}")


def _token_meta(record: TokenRecord) -> TokenMetaResponse:
    """Shape a credential's metadata; the value never leaves the service."""
    return TokenMetaResponse(
        id=record.id or 0,
        token_name=record.name,
        scope=record.scope,
        is_active=record.is_active,
        expires_at=record.expires_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


@router.get("/tokens", response_model=list[TokenMetaResponse])
async def list_tokens(
    service=Depends(get_token_service),
) -> list[TokenMetaResponse]:
    """List token metadata; token values are never included."""
    return [
        TokenMetaResponse(
            id=item["id"] or 0,
            token_name=item["token_name"],
            scope=item["scope"],
            is_active=item["is_active"],
            expires_at=item["expires_at"],
            created_at=item["created_at"],
            updated_at=item["updated_at"],
        )
        for item in await service.list_tokens()
    ]


@router.post(
    "/tokens",
    response_model=TokenMetaResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_token(
    request: AddTokenRequest,
) -> TokenMetaResponse:
    """Register a credential.

    Binding repositories is a separate operation (``POST /repositories``), so
    this endpoint stores the credential only: a credential exists independently
    of which repositories it happens to cover today.
    """
    from ....core import exceptions as core_errors

    service = get_token_service()
    try:
        record = await service.add_token(name=request.name, token=request.token)
    except core_errors.CitrackerError as e:
        raise _map_token_error(request.name, e) from e
    return _token_meta(record)


@router.post("/tokens/{name}/verify")
async def verify_token(
    name: str,
    service=Depends(get_token_service),
) -> dict[str, Any]:
    """Probe what a stored token can do against GitHub."""
    from ....core import exceptions as core_errors

    try:
        return await service.verify_token(name=name)
    except core_errors.CitrackerError as e:
        raise _map_token_error(name, e) from e


@router.patch("/tokens/{name}", response_model=TokenMetaResponse)
async def rotate_token(
    name: str,
    request: RotateTokenRequest,
) -> TokenMetaResponse:
    """Rotate a credential's value, keeping its identity and bindings."""
    from ....core import exceptions as core_errors

    service = get_token_service()
    try:
        record = await service.rotate_token(
            name=name, token=request.token, expires_at=request.expires_at
        )
    except core_errors.CitrackerError as e:
        raise _map_token_error(name, e) from e
    return _token_meta(record)


@router.delete("/tokens/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_token(
    name: str,
    service=Depends(get_token_service),
) -> None:
    """Delete a token permanently; 409 while repositories still use it."""
    from ....core import exceptions as core_errors

    try:
        await service.delete_token(name=name)
    except core_errors.CitrackerError as e:
        raise _map_token_error(name, e) from e
