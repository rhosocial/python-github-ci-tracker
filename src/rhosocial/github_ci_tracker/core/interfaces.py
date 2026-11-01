# src/rhosocial/github_ci_tracker/core/interfaces.py
"""The GitHub protocol every service depends on.

Services name this protocol instead of
:class:`~rhosocial.github_ci_tracker.services.github.client.GithubClient`, so tests can
inject a fake and the HTTP layer can be swapped without touching business logic.
The narrower per-group protocols the services actually consume live next to
them, in :mod:`rhosocial.github_ci_tracker.services.github.protocols`.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class IGithubClient(Protocol):
    """GitHub REST endpoints used by the core services."""

    async def trigger_workflow(
        self,
        token: str,
        repo_full_name: str,
        workflow_id: str,
        ref: str,
        inputs: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST .../actions/workflows/{workflow_id}/dispatches."""
        ...

    async def get_run(
        self,
        token: str,
        repo_full_name: str,
        run_id: int,
    ) -> dict[str, Any]:
        """GET .../actions/runs/{run_id}."""
        ...

    async def list_runs(
        self,
        token: str,
        repo_full_name: str,
        workflow_id: str | None = None,
        status: str | None = None,
        branch: str | None = None,
        per_page: int = 30,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """GET .../actions/runs."""
        ...

    async def get_run_jobs(
        self,
        token: str,
        repo_full_name: str,
        run_id: int,
    ) -> list[dict[str, Any]]:
        """GET .../actions/runs/{run_id}/jobs."""
        ...

    async def get_job(
        self,
        token: str,
        repo_full_name: str,
        job_id: int,
    ) -> dict[str, Any]:
        """GET .../actions/jobs/{job_id}."""
        ...

    async def get_run_logs(
        self,
        token: str,
        repo_full_name: str,
        run_id: int,
    ) -> bytes:
        """GET .../actions/runs/{run_id}/logs -- the zip body, unparsed."""
        ...

    async def get_job_logs(
        self,
        token: str,
        repo_full_name: str,
        job_id: int,
    ) -> str:
        """GET .../actions/jobs/{job_id}/logs."""
        ...

    async def get_repo_workflows(
        self,
        token: str,
        repo_full_name: str,
    ) -> list[dict[str, Any]]:
        """GET .../actions/workflows."""
        ...

    async def create_webhook(
        self,
        token: str,
        repo_full_name: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """POST .../hooks."""
        ...

    async def delete_webhook(
        self,
        token: str,
        repo_full_name: str,
        hook_id: int,
    ) -> None:
        """DELETE .../hooks/{hook_id}."""
        ...

    async def list_webhooks(
        self,
        token: str,
        repo_full_name: str,
    ) -> list[dict[str, Any]]:
        """GET .../hooks."""
        ...

    async def verify_token(self, token: str) -> dict[str, Any]:
        """Probe ``/user`` and ``/user/repos`` to describe a token."""
        ...


__all__ = [
    "IGithubClient",
]
