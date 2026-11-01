# src/rhosocial/github_ci_tracker/adapters/cli/commands/webhook.py
"""rhosocial-github-ci-tracker webhook command."""

from __future__ import annotations

import argparse
from typing import Any

from ..formatters import format_output


async def run(args: argparse.Namespace, container: Any) -> None:
    """Execute the webhook command.

    Args:
        args: Parsed command-line arguments
        container: The controllers this process built.
    """

    service = container.webhook

    if args.webhook_command == "create":
        webhook = await service.create_webhook(repo_full_name=args.name)
        print(f"Webhook created for '{args.name}': {webhook}")
    elif args.webhook_command == "delete":
        await service.delete_webhook(repo_full_name=args.name)
        print(f"Webhook deleted for '{args.name}'.")
    elif args.webhook_command == "list":
        webhooks = await service.list_webhooks(repo_full_name=args.name)
        data = {"webhooks": webhooks}
        print(format_output(data, args.format, args.verbose))
    elif args.webhook_command == "ping":
        result = await service.ping_webhook(repo_full_name=args.name)
        print(f"Ping result: {result}")
    else:
        import sys

        print("Error: No webhook subcommand specified", file=sys.stderr)
