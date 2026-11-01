# src/rhosocial/github_ci_tracker/adapters/cli/commands/latest.py
"""rhosocial-github-ci-tracker latest command."""

from __future__ import annotations

import argparse
from typing import Any

from ...common.errors import CitrackerError
from ..formatters import format_run_status


async def run(args: argparse.Namespace, container: Any) -> None:
    """Execute the latest command.

    Args:
        args: Parsed command-line arguments
        container: The controllers this process built.
    """

    service = container.query
    try:
        record = await service.get_latest_run(
            repository=args.repo,
            branch=args.branch,
            workflow=args.workflow,
        )
    except CitrackerError as exc:
        print(f"Error: {exc}", flush=True)
        raise SystemExit(1) from exc

    if record is None:
        branch_hint = args.branch or "the default branch"
        workflow_hint = f" of workflow '{args.workflow}'" if args.workflow else ""
        print(f"No CI runs found for '{args.repo}' on {branch_hint}{workflow_hint}.")
        return

    output = format_run_status(
        {
            "run_id": record.run_id,
            "run_number": record.run_number,
            "status": record.status,
            "conclusion": record.conclusion,
            "branch": record.branch,
            "commit_sha": record.commit_sha,
            "commit_message": record.commit_message,
            "actor": record.actor,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "started_at": record.started_at.isoformat() if record.started_at else None,
            "completed_at": record.completed_at.isoformat() if record.completed_at else None,
            "html_url": record.html_url,
        },
        args.format,
    )
    print(output)
