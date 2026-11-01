# src/rhosocial/github_ci_tracker/adapters/rest/routes/jobs.py
"""Job routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request

from ...common.errors import NotFoundError
from ..schemas.responses import (
    JobDetailResponse,
    JobLogsResponse,
    StepLogSection,
    StepSummary,
)

if TYPE_CHECKING:
    from ....views import JobLogsView, JobRecord

router = APIRouter()


def get_query_service(request: Request):
    """Dependency: the query controller this application serves from."""
    return request.app.state.container.query


def _job_response(job: JobRecord) -> JobDetailResponse:
    """Project a facade ``JobRecord`` onto the REST response model.

    The mapping stays explicit field by field -- the facade already speaks in
    ``job_id`` / ``run_id``, the derived ``duration_seconds`` is read from the
    record's property, and a renamed or dropped facade field fails loudly here
    instead of silently vanishing from the payload.
    """
    return JobDetailResponse(
        job_id=job.job_id,
        run_id=job.run_id,
        name=job.name,
        status=job.status,
        conclusion=job.conclusion,
        runner_name=job.runner_name,
        started_at=job.started_at,
        completed_at=job.completed_at,
        duration_seconds=job.duration_seconds,
        steps=[
            StepSummary(
                step_id=step.step_id,
                number=step.number,
                name=step.name,
                status=step.status,
                conclusion=step.conclusion,
                started_at=step.started_at,
                completed_at=step.completed_at,
                duration_seconds=step.duration_seconds,
                has_log=step.has_log,
                log_lines=step.log_lines,
            )
            for step in job.steps
        ],
    )


def _logs_response(logs: JobLogsView) -> JobLogsResponse:
    """Project a facade ``JobLogsView`` onto the REST response model.

    ``line_count`` is presentation-only: the facade carries the raw log text,
    the REST view derives the count from it.
    """
    return JobLogsResponse(
        job_id=logs.job_id,
        job_name=logs.job_name,
        log_content=logs.log_content,
        line_count=len(logs.log_content.splitlines()),
        source=getattr(logs, "source", "job"),
        sections=[
            StepLogSection(
                step=section.number,
                name=section.name,
                conclusion=section.conclusion,
                content=section.content,
                lines=section.line_count,
            )
            for section in getattr(logs, "sections", [])
        ],
        steps_without_logs=list(getattr(logs, "steps_without_logs", [])),
        reason=getattr(logs, "reason", None),
    )


@router.get("/runs/{run_id}/jobs", response_model=list[JobDetailResponse])
async def get_run_jobs(
    run_id: int,
    repository: str,
    service=Depends(get_query_service),
) -> list[JobDetailResponse]:
    """Get jobs for a run."""
    jobs = await service.get_run_jobs(
        repository=repository,
        run_id=run_id,
    )
    return [_job_response(job) for job in jobs]


@router.get("/jobs/{job_id}", response_model=JobDetailResponse)
async def get_job_detail(
    job_id: int,
    repository: str,
    service=Depends(get_query_service),
) -> JobDetailResponse:
    """Get job detail."""
    try:
        job = await service.get_job_detail(
            repository=repository,
            job_id=job_id,
        )
    except Exception as e:
        raise NotFoundError("job", str(job_id)) from e
    return _job_response(job)


@router.get("/jobs/{job_id}/logs", response_model=JobLogsResponse)
async def get_job_logs(
    job_id: int,
    repository: str,
    tail: int | None = None,
    step: int | None = None,
    failed_steps_only: bool = False,
    service=Depends(get_query_service),
) -> JobLogsResponse:
    """Get job logs: the merged document, one step, or the failed steps.

    The merged document is what this endpoint has always returned. ``step`` and
    ``failed_steps_only`` read the captured step logs instead: text that has to
    be taken while GitHub still splits a run into per-step files (about an hour
    after it finishes), and that is then stored here permanently. A step that
    was never captured is reported as missing rather than replaced by text from
    somewhere else.
    """
    try:
        logs = await service.get_job_logs(
            repository=repository,
            job_id=job_id,
            tail=tail,
            step=step,
            failed_steps_only=failed_steps_only,
        )
    except Exception as e:
        raise NotFoundError("job", str(job_id)) from e
    return _logs_response(logs)
