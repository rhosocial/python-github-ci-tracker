# src/rhosocial/github_ci_tracker/adapters/rest/schemas/requests.py
"""Request models for REST API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TriggerWorkflowRequest(BaseModel):
    """Trigger workflow request."""

    repository: str = Field(..., description="Repository full name, e.g. 'owner/repo'")
    ref: str = Field(..., description="Branch or tag name")
    inputs: dict[str, str] | None = Field(None, description="Workflow input parameters")
    commit_sha: str | None = Field(
        None,
        description=(
            "Commit if_absent checks; defaults to the ref's head, which needs "
            "Contents read on the credential"
        ),
    )
    if_absent: bool = Field(
        False,
        description=(
            "Return the run this workflow already has for the ref's head commit "
            "instead of dispatching again; the response then says dispatched=false"
        ),
    )


class SubscribeRepositoryRequest(BaseModel):
    """Subscribe repository request."""

    full_name: str = Field(..., description="Repository full name")
    token_name: str = Field(
        ...,
        description=(
            "Credential to bind: the token name this API reports (`work`), or the "
            "`kind/name` reference other surfaces show (`pat/work`)"
        ),
    )


class AddTokenRequest(BaseModel):
    """Add token request."""

    name: str = Field(..., description="Token name")
    token: str = Field(..., description="GitHub fine-grained PAT")
    repos: list[str] | None = Field(
        None,
        description="Repositories the token covers ('owner/repo'); each is subscribed "
        "and bound to this token",
    )
    expires_at: datetime | None = Field(None, description="Declared expiry of the token")


class RotateTokenRequest(BaseModel):
    """Rotate token request (replace the value, keep the identity)."""

    token: str = Field(..., description="New GitHub fine-grained PAT value")
    expires_at: datetime | None = Field(
        None,
        description="New declared expiry; omitted keeps the previous expiry",
    )
