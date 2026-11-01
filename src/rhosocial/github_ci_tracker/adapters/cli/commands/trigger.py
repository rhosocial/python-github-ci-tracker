# src/rhosocial/github_ci_tracker/adapters/cli/commands/trigger.py
"""rhosocial-github-ci-tracker trigger command."""

from __future__ import annotations

import argparse
from typing import Any

from ..formatters import format_trigger_result


async def run(args: argparse.Namespace, container: Any) -> None:
    """Execute the trigger command.

    Args:
        args: Parsed command-line arguments
        container: The controllers this process built.
    """
    # Parse input parameters
    inputs: dict[str, str] = {}
    for item in args.input:
        if "=" in item:
            key, value = item.split("=", 1)
            inputs[key] = value

    # Call business service

    service = container.trigger
    result = await service.trigger_workflow(
        repository=args.repo,
        workflow=args.workflow,
        ref=args.ref,
        inputs=inputs if inputs else None,
        if_absent=getattr(args, "if_absent", False),
        commit_sha=getattr(args, "commit_sha", None),
    )

    # Format and print output
    output = format_trigger_result(
        {
            "run_id": result.run_id,
            "status": result.status,
            "attempt": result.attempt,
            "message": result.message,
            "dispatched": result.dispatched,
            "html_url": result.html_url,
        },
        args.format,
    )
    print(output)

    # Wait for completion if requested
    if args.wait:
        status = await service.wait_for_completion(
            repository=args.repo,
            run_id=result.run_id,
            timeout=args.wait,
        )
        print(f"\nFinal status: {status.status} ({status.conclusion or 'N/A'})")
