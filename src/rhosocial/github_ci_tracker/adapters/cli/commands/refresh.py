# src/rhosocial/github_ci_tracker/adapters/cli/commands/refresh.py
"""rhosocial-github-ci-tracker refresh command."""

from __future__ import annotations

import argparse
from typing import Any

from ...common.errors import CitrackerError
from ..formatters import format_output


async def run(args: argparse.Namespace, container: Any) -> None:
    """Execute the refresh command.

    Args:
        args: Parsed command-line arguments
        container: The controllers this process built.
    """

    service = container.query
    try:
        result = await service.refresh_run(
            repository=args.repo,
            run_id=args.run_id,
        )
    except CitrackerError as exc:
        print(f"Error: {exc}", flush=True)
        raise SystemExit(1) from exc

    print(f"Run {result.run_id} (#{result.run_number}) synchronized from GitHub.")
    print(f"  status:     {result.previous_status or '(new)'} -> {result.status}")
    print(f"  conclusion: {result.conclusion or '-'}")
    print(f"  jobs:       {result.jobs_synced} synced")
    print(f"  steps:      {result.steps_synced} synced")
    print(f"  url:        {result.html_url}")

    output = format_output(
        {
            "run_id": result.run_id,
            "run_number": result.run_number,
            "previous_status": result.previous_status,
            "status": result.status,
            "conclusion": result.conclusion,
            "created": result.created,
            "changed": result.changed,
            "jobs_synced": result.jobs_synced,
            "steps_synced": result.steps_synced,
            "html_url": result.html_url,
        },
        args.format,
    )
    if args.format == "json":
        print(output)
