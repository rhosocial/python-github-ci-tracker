# src/rhosocial/github_ci_tracker/controllers/workflow.py
"""A workflow-shaped entry point over the repository and trigger flows.

Some protocols present their tools by workflow rather than by repository, so
this class exists to offer that shape. Every method forwards; it deliberately
holds no logic of its own, or it would become a second implementation of
whichever controller it wraps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..views import RunView, RunWaitView
from .repository import RepositoryController
from .trigger import TriggerController

if TYPE_CHECKING:
    from ..models import AsyncWorkflow


class WorkflowController:
    """AsyncWorkflow discovery and triggering."""

    def __init__(
        self,
        *,
        repositories: RepositoryController,
        trigger: TriggerController,
    ) -> None:
        self._repositories = repositories
        self._trigger = trigger

    async def list_workflows(self, repository: str) -> list[AsyncWorkflow]:
        """List the workflow definitions of a repository."""
        return await self._repositories.list_workflows(repository=repository)

    async def get_workflow(self, repository: str, workflow: str) -> AsyncWorkflow:
        """Resolve one workflow by name, path or id."""
        return await self._repositories.get_workflow(repository=repository, workflow=workflow)

    async def trigger_workflow(
        self,
        repository: str,
        workflow: str,
        ref: str,
        inputs: dict[str, str] | None = None,
        *,
        if_absent: bool = False,
        commit_sha: str | None = None,
    ) -> RunView:
        """Trigger a workflow run, or return the one this commit already has."""
        return await self._trigger.trigger_workflow(
            repository=repository,
            if_absent=if_absent,
            commit_sha=commit_sha,
            workflow=workflow,
            ref=ref,
            inputs=inputs,
        )

    async def rerun_workflow(self, repository: str, run_id: int) -> RunView:
        """Re-run a previous run."""
        return await self._trigger.rerun_workflow(repository=repository, run_id=run_id)

    async def wait_for_completion(
        self,
        repository: str,
        run_id: int,
        timeout: float | None = 600.0,
        poll_interval: float = 5.0,
    ) -> RunWaitView:
        """Wait for a run to reach a terminal state."""
        return await self._trigger.wait_for_completion(
            repository=repository,
            run_id=run_id,
            timeout=timeout,
            poll_interval=poll_interval,
        )
