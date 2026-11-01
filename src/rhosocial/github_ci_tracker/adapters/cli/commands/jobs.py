# src/rhosocial/github_ci_tracker/adapters/cli/commands/jobs.py
"""rhosocial-github-ci-tracker jobs command."""

from __future__ import annotations

import argparse
from typing import Any

from ..formatters import format_output


async def run(args: argparse.Namespace, container: Any) -> None:
    """Execute the jobs command.

    Args:
        args: Parsed command-line arguments
        container: The controllers this process built.
    """

    service = container.query
    jobs = await service.get_run_jobs(
        repository=args.repo,
        run_id=args.run_id,
    )

    data = {
        "jobs": [
            {
                "job_id": job.job_id,
                "name": job.name,
                "status": job.status,
                "conclusion": job.conclusion,
                "runner": job.runner_name,
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "completed_at": job.completed_at.isoformat() if job.completed_at else None,
                "duration_seconds": job.duration_seconds,
            }
            for job in jobs
        ]
    }

    output = format_output(data, args.format, args.verbose)
    print(output)
