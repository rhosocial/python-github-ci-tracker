# src/rhosocial/github_ci_tracker/adapters/rest/routes/runs.py
"""Run routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Query, Request

from ...common.errors import NotFoundError
from ..schemas.responses import JobSummary, RunRefreshResponse, RunStatusResponse

if TYPE_CHECKING:
    from ....views import RunRecord, RunRefreshView

router = APIRouter()


def get_query_service(request: Request):
    """Dependency: the query controller this application serves from."""
    return request.app.state.container.query


def _run_response(run: RunRecord) -> RunStatusResponse:
    """Project a facade ``RunRecord`` onto the REST response model.

    The mapping stays explicit field by field: the facade already speaks in
    ``run_id`` / ``html_url``, and a renamed or dropped facade field then
    fails loudly here instead of silently vanishing from the payload.
    """
    return RunStatusResponse(
        run_id=run.run_id,
        run_number=run.run_number,
        attempt=run.attempt,
        is_latest=run.is_latest,
        attempts_total=run.attempts_total,
        attempts_cached=run.attempts_cached,
        cache_state=run.cache_state,
        status=run.status,
        conclusion=run.conclusion,
        branch=run.branch,
        workflow_name=run.workflow_name,
        workflow_path=run.workflow_path,
        commit_sha=run.commit_sha,
        commit_message=run.commit_message,
        commit_author=run.commit_author,
        commit_time=run.commit_time,
        display_title=run.display_title,
        head_repo=run.head_repo,
        base_ref=run.base_ref or None,
        pr_number=run.pr_number,
        actor=run.actor,
        event=run.event,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        stored_at=run.stored_at,
        html_url=run.html_url,
        duration_seconds=run.duration_seconds,
        jobs=[
            JobSummary(
                job_id=job.job_id,
                name=job.name,
                status=job.status,
                conclusion=job.conclusion,
            )
            for job in run.jobs
        ],
    )


def _refresh_response(view: RunRefreshView) -> RunRefreshResponse:
    """Project a facade ``RunRefreshView`` onto the REST response model."""
    return RunRefreshResponse(
        run_id=view.run_id,
        run_number=view.run_number,
        previous_status=view.previous_status,
        status=view.status,
        conclusion=view.conclusion,
        created=view.created,
        changed=view.changed,
        jobs_synced=view.jobs_synced,
        steps_synced=view.steps_synced,
        html_url=view.html_url,
    )


@router.get("/runs", response_model=list[RunStatusResponse])
async def list_runs(
    repository: str,
    workflow: str | None = None,
    branch: str | None = None,
    status: str | None = None,
    per_page: int = Query(30, ge=1, le=100),
    page: int = Query(1, ge=1),
    service=Depends(get_query_service),
) -> list[RunStatusResponse]:
    """List workflow runs."""
    runs = await service.list_runs(
        repository=repository,
        workflow=workflow,
        branch=branch,
        status=status,
        per_page=per_page,
        page=page,
    )
    return [_run_response(run) for run in runs]


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
async def get_run_status(
    run_id: int,
    repository: str,
    attempt: int | None = None,
    service=Depends(get_query_service),
) -> RunStatusResponse:
    """Get run status, for the newest attempt or the one named by ``attempt``.

    The core exceptions travel to the app-level handler unchanged: a missing run
    is a 404, an attempt number past the last one is a 404 that names the range,
    and a credential or GitHub failure keeps its own status instead of being
    flattened into "run not found".
    """
    run = await service.get_run_status(
        repository=repository,
        run_id=run_id,
        attempt=attempt,
    )
    return _run_response(run)


@router.post("/runs/{run_id}/refresh", response_model=RunRefreshResponse)
async def refresh_run(
    run_id: int,
    repository: str,
    service=Depends(get_query_service),
) -> RunRefreshResponse:
    """Force a sync of one run from GitHub.

    The service is fully passive: run state enters the database only through a
    GitHub webhook delivery or through this call. Unlike ``GET /runs/{run_id}``
    this endpoint never trusts a terminal local record, and it adopts a run the
    system has never seen -- so a missed webhook delivery can always be
    recovered here.
    """
    try:
        result = await service.refresh_run(
            repository=repository,
            run_id=run_id,
        )
    except Exception as e:
        raise NotFoundError("run", str(run_id)) from e
    return _refresh_response(result)


@router.get("/latest-run", response_model=RunStatusResponse | None)
async def get_latest_run(
    repository: str,
    branch: str | None = None,
    workflow: str | None = None,
    service=Depends(get_query_service),
) -> RunStatusResponse | None:
    """Get the newest run of a repository.

    ``branch`` and ``workflow`` narrow it; omitting them means "newest run
    overall", not "newest on the default branch". A run of another branch is
    still this repository's news, and answering with an older default-branch run
    while ignoring it reads as authoritative but wrong.
    """
    run = await service.get_latest_run(
        repository=repository,
        branch=branch,
        workflow=workflow,
    )
    if run is None:
        return None
    return _run_response(run)


@router.get("/pull-request-runs", response_model=list[RunStatusResponse])
async def get_pull_request_runs(
    repository: str,
    number: int,
    request: Request,
) -> list[RunStatusResponse]:
    """List the CI runs attributed to a pull request.

    GitHub has no PR-number filter on the runs endpoint, so the lookup reads
    the pull request for its head commit and then attributes the runs found for
    that commit. An empty list is a valid answer (a pull request with no runs,
    or one not visible to the bound token).
    """
    service = request.app.state.container.repository
    runs = await service.pull_request_runs(repository=repository, number=number)
    return [_run_response(run) for run in runs]
