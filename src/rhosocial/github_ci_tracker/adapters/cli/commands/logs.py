# src/rhosocial/github_ci_tracker/adapters/cli/commands/logs.py
"""rhosocial-github-ci-tracker logs command."""

from __future__ import annotations

import argparse
from typing import Any


async def run(args: argparse.Namespace, container: Any) -> None:
    """Execute the logs command.

    Args:
        args: Parsed command-line arguments
        container: The controllers this process built.
    """

    service = container.query
    logs = await service.get_job_logs(
        repository=args.repo,
        job_id=args.job_id,
        tail=args.tail,
        step=getattr(args, "step", None),
        failed_steps_only=getattr(args, "failed_steps", False),
    )

    text = logs.log_content
    sections = getattr(logs, "sections", [])
    if sections:
        # Step mode: keep the boundary visible, because the point of asking for
        # a step is knowing where it ends.
        blocks = [
            f"===== step {section.number}: {section.name} "
            f"({section.conclusion or 'no conclusion'}) =====\n{section.content}"
            for section in sections
        ]
        text = "\n".join(blocks)
    elif getattr(logs, "reason", None):
        text = (
            f"No captured step log ({logs.reason}). "
            "Read the whole job log instead (omit --step / --failed-steps)."
        )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Logs written to {args.output}")
    else:
        print(text)
