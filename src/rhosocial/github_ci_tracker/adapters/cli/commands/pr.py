# src/rhosocial/github_ci_tracker/adapters/cli/commands/pr.py
"""rhosocial-github-ci-tracker pr command."""

from __future__ import annotations

import argparse
from typing import Any

from ...common.errors import CitrackerError
from ..formatters import format_output


async def run(args: argparse.Namespace, container: Any) -> None:
    """Execute the pr command.

    Args:
        args: Parsed command-line arguments
        container: The controllers this process built.
    """

    service = container.repository
    try:
        runs = await service.pull_request_runs(
            repository=args.repo,
            number=args.pr,
        )
    except CitrackerError as exc:
        print(f"Error: {exc}", flush=True)
        raise SystemExit(1) from exc

    if not runs:
        print(
            f"No CI runs found for pull request #{args.pr} of '{args.repo}'. "
            "(An open pull request with no runs, or one not visible to the bound token.)"
        )
        return

    print(f"Pull request #{args.pr} of '{args.repo}': {len(runs)} run(s)")
    for run in runs:
        print(
            f"  run {run.run_id} #{run.run_number} {run.status}/{run.conclusion or '-'} "
            f"branch={run.branch}"
        )

    if args.format == "json":
        print(
            format_output(
                {
                    "pull_request": args.pr,
                    "runs": [
                        {
                            "run_id": run.run_id,
                            "run_number": run.run_number,
                            "status": run.status,
                            "conclusion": run.conclusion,
                            "branch": run.branch,
                            "html_url": run.html_url,
                        }
                        for run in runs
                    ],
                },
                args.format,
            )
        )
