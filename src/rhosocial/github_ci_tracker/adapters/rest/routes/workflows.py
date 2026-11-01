# src/rhosocial/github_ci_tracker/adapters/rest/routes/workflows.py
"""Workflow routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status

from ...common.errors import NotFoundError
from ..schemas.requests import TriggerWorkflowRequest
from ..schemas.responses import TriggerWorkflowResponse, WorkflowResponse

router = APIRouter()


def get_workflow_service(request: Request):
    """Dependency: the workflow controller this application serves from."""
    return request.app.state.container.workflow


@router.get("/workflows", response_model=list[WorkflowResponse])
async def list_workflows(
    repository: str,
    service=Depends(get_workflow_service),
) -> list[WorkflowResponse]:
    """List workflows for a repository."""
    workflows = await service.list_workflows(repository=repository)
    return [
        WorkflowResponse(
            id=wf.id,
            workflow_id=wf.workflow_id,
            name=wf.name,
            path=wf.path,
            state=wf.state,
        )
        for wf in workflows
    ]


@router.get("/workflows/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(
    workflow_id: int,
    repository: str,
    service=Depends(get_workflow_service),
) -> WorkflowResponse:
    """Get workflow details."""
    try:
        # The facade resolves workflows by name, path or numeric id; the path
        # parameter is the GitHub workflow id, passed through as the reference.
        wf = await service.get_workflow(
            repository=repository,
            workflow=str(workflow_id),
        )
    except Exception as e:
        raise NotFoundError("workflow", str(workflow_id)) from e
    return WorkflowResponse(
        id=wf.id,
        workflow_id=wf.workflow_id,
        name=wf.name,
        path=wf.path,
        state=wf.state,
    )


@router.post(
    "/workflows/{workflow_id}/trigger",
    response_model=TriggerWorkflowResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_workflow(
    workflow_id: int,
    request: TriggerWorkflowRequest,
    service=Depends(get_workflow_service),
) -> TriggerWorkflowResponse:
    """Trigger a workflow run.

    This handler performs no error translation of its own; the app-level
    handler for the core exception hierarchy turns an unknown repository or
    workflow into 404 and a credential problem into 403 -- the caller asked for
    something the deployment does not permit rather than sending a malformed
    request.
    """
    result = await service.trigger_workflow(
        repository=request.repository,
        workflow=str(workflow_id),
        ref=request.ref,
        inputs=request.inputs,
        if_absent=request.if_absent,
        commit_sha=request.commit_sha,
    )
    return TriggerWorkflowResponse(
        run_id=result.run_id,
        status=result.status,
        message=result.message,
        html_url=result.html_url,
        conclusion=result.conclusion,
        attempt=result.attempt,
        dispatched=result.dispatched,
    )
