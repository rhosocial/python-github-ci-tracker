# src/rhosocial/github_ci_tracker/adapters/cli/commands/status.py
"""rhosocial-github-ci-tracker status command."""

from __future__ import annotations

import argparse
from typing import Any

from ..formatters import format_run_status


async def run(args: argparse.Namespace, container: Any) -> None:
    """Execute the status command.

    Args:
        args: Parsed command-line arguments
        container: The controllers this process built.
    """

    service = container.query
    status = await service.get_run_status(
        repository=args.repo,
        run_id=args.run_id,
    )

    output = format_run_status(
        {
            "run_id": status.run_id,
            "status": status.status,
            "conclusion": status.conclusion,
            "branch": status.branch,
            "commit_sha": status.commit_sha,
            "commit_message": status.commit_message,
            "actor": status.actor,
            "started_at": status.started_at.isoformat() if status.started_at else None,
            "completed_at": status.completed_at.isoformat() if status.completed_at else None,
            "duration_seconds": status.duration_seconds,
            "jobs": [
                {
                    "name": job.name,
                    "status": job.status,
                    "conclusion": job.conclusion,
                }
                for job in status.jobs
            ],
        },
        args.format,
    )
    print(output)
