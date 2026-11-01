# src/rhosocial/github_ci_tracker/adapters/rest/routes/repositories.py
"""Repository routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status

from ...common.errors import DuplicateResourceError, NotFoundError
from ..schemas.requests import SubscribeRepositoryRequest
from ..schemas.responses import RepositoryResponse

router = APIRouter()


def get_repository_service(request: Request):
    """Dependency: the repository controller this application serves from."""
    return request.app.state.container.repository


def get_token_service(request: Request):
    """Dependency: the credential controller this application serves from."""
    return request.app.state.container.token


async def _resolve_credential_ref(token_name: str, tokens) -> int:
    """Resolve a token alias to the database id the facade binds by.

    The REST API identifies tokens by name -- ``TokenMetaResponse`` never
    carries the token value, and callers never handle surrogate ids -- while
    the facade ``RepositoryController.subscribe_repository`` speaks in token
    ids. The lookup goes through the facade's own token listing, so the
    resolved id always refers to a token the facade can see.

    Raises:
        NotFoundError: If no token is registered under the name.
    """
    listed = await tokens.list_tokens()
    wanted = token_name.strip()
    # Both spellings name the same credential: the bare one this API reports
    # (`work`) and the `kind/name` one every other surface shows (`pat/work`).
    # A caller learns the second spelling from ci_status or the CLI and then
    # cannot use it here, so accept both rather than report the token missing.
    bare = wanted.split("/", 1)[1].strip() if "/" in wanted else wanted
    for item in listed:
        if item["token_name"] == bare:
            return int(item["id"] or 0)
    raise NotFoundError("token", token_name)


@router.get("/repositories", response_model=list[RepositoryResponse])
async def list_repositories(
    service=Depends(get_repository_service),
) -> list[RepositoryResponse]:
    """List subscribed repositories."""
    repos = await service.list_repositories()
    return [
        RepositoryResponse(
            id=repo.id,
            full_name=repo.full_name,
            owner=repo.owner,
            name=repo.name,
            is_private=repo.is_private,
            default_branch=repo.default_branch,
            subscribed_at=repo.created_at,
        )
        for repo in repos
    ]


@router.post(
    "/repositories",
    response_model=RepositoryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def subscribe_repository(
    request: SubscribeRepositoryRequest,
    service=Depends(get_repository_service),
    tokens=Depends(get_token_service),
) -> RepositoryResponse:
    """Subscribe to a repository."""
    try:
        repo = await service.subscribe_repository(
            full_name=request.full_name,
            token_id=await _resolve_credential_ref(request.token_name, tokens),
        )
    except ValueError as e:
        raise DuplicateResourceError("repository", request.full_name) from e
    return RepositoryResponse(
        id=repo.id,
        full_name=repo.full_name,
        owner=repo.owner,
        name=repo.name,
        is_private=repo.is_private,
        default_branch=repo.default_branch,
        subscribed_at=repo.created_at,
    )


@router.delete("/repositories/{repo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe_repository(
    repo_id: int,
    service=Depends(get_repository_service),
) -> None:
    """Unsubscribe from a repository."""
    try:
        await service.unsubscribe_repository(repo_id=repo_id)
    except Exception as e:
        raise NotFoundError("repository", str(repo_id)) from e
