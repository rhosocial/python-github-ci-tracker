# src/rhosocial/github_ci_tracker/adapters/cli/commands/runs.py
"""rhosocial-github-ci-tracker runs command."""

from __future__ import annotations

import argparse
from typing import Any

from ..formatters import format_output


async def run(args: argparse.Namespace, container: Any) -> None:
    """Execute the runs command.

    Args:
        args: Parsed command-line arguments
        container: The controllers this process built.
    """

    service = container.query
    runs = await service.list_runs(
        repository=args.repo,
        workflow=args.workflow,
        branch=args.branch,
        status=args.status,
        per_page=args.limit,
    )

    data = {
        "runs": [
            {
                "run_id": run.run_id,
                "run_number": run.run_number,
                "status": run.status,
                "conclusion": run.conclusion,
                "branch": run.branch,
                "commit_message": run.commit_message,
                "created_at": run.created_at.isoformat() if run.created_at else None,
            }
            for run in runs
        ]
    }

    output = format_output(data, args.format, args.verbose)
    print(output)
