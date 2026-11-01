# src/rhosocial/github_ci_tracker/adapters/cli/main.py
"""Project-level CLI: the protocol adapter for humans.

This is one of the three protocol faces of the service (REST, MCP, CLI) and is
functionally identical to the REST API and the MCP tools — it parses
arguments, builds the process container, and calls the controllers. It is *not* a
debugging tool; for debugging a single module, run that module directly:

    python -m rhosocial.github_ci_tracker.adapters.cli.github_debug --help
    python -m rhosocial.github_ci_tracker.adapters.cli.db         --help
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from .commands import (
    credential,
    jobs,
    latest,
    logs,
    pr,
    refresh,
    repo,
    runs,
    serve,
    status,
    trigger,
    webhook,
)
from .eventloop import run_coroutine

#: One command: the parsed arguments and the container the entry point built.
Runner = Callable[[argparse.Namespace, Any], Awaitable[None]]

#: Commands that read or write through the database. ``serve`` is not here: it
#: owns its own loop for the lifetime of the server.
RUNNERS: dict[str, Runner] = {
    "trigger": trigger.run,
    "status": status.run,
    "jobs": jobs.run,
    "logs": logs.run,
    "runs": runs.run,
    "latest": latest.run,
    "refresh": refresh.run,
    "pr": pr.run,
    "credential": credential.run,
    "repo": repo.run,
    "webhook": webhook.run,
}


def _run(coro) -> None:
    """Run a coroutine on a loop psycopg accepts on Windows."""
    run_coroutine(coro)


def _run_with_db(runner: Runner, args: argparse.Namespace) -> None:
    """Run a command inside a database scope, with a container built for it.

    The REST adapter opens a scope per request and the MCP app opens one per
    call; this CLI has neither, so every DB-touching command opens its own. The
    scope publishes the backend on the ContextVar the models read, so the
    command's controllers all resolve to the same connection.

    The container is built **without** an ambient credential: this CLI creates
    the very credentials and bindings a server later reads, so each repository's
    binding decides which token answers.
    """

    async def body() -> None:
        from ...container import Container
        from ...database import Database

        database = await Database.create()
        try:
            async with database.connection():
                await runner(args, Container.build(database=database))
        finally:
            await database.close()

    _run(body())


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        prog="rhosocial-github-ci-tracker",
        description="GitHub CI status tracking and automation service",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API Key (or set CITRACKER_API_KEY environment variable)",
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=["text", "json"],
        default="text",
        help="Output format",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose output",
    )

    subparsers = parser.add_subparsers(dest="command")

    # trigger
    trigger_parser = subparsers.add_parser("trigger", help="Trigger workflow")
    trigger_parser.add_argument("--repo", "-r", required=True)
    trigger_parser.add_argument("--workflow", "-w", required=True)
    trigger_parser.add_argument("--ref", required=True)
    trigger_parser.add_argument("--input", "-i", action="append", default=[])
    trigger_parser.add_argument("--wait", type=int, default=None)
    trigger_parser.add_argument(
        "--commit-sha",
        default=None,
        help="Commit the --if-absent check uses (default: the ref's head)",
    )
    trigger_parser.add_argument(
        "--if-absent",
        action="store_true",
        help=(
            "Return the run already created for the ref's head commit instead of "
            "dispatching a second one"
        ),
    )

    # status
    status_parser = subparsers.add_parser("status", help="Get run status")
    status_parser.add_argument("--repo", "-r", required=True)
    status_parser.add_argument("--run-id", required=True, type=int)

    # jobs
    jobs_parser = subparsers.add_parser("jobs", help="List run jobs")
    jobs_parser.add_argument("--repo", "-r", required=True)
    jobs_parser.add_argument("--run-id", required=True, type=int)

    # logs
    logs_parser = subparsers.add_parser("logs", help="Get job logs")
    logs_parser.add_argument("--repo", "-r", required=True)
    logs_parser.add_argument("--job-id", required=True, type=int)
    logs_parser.add_argument("--tail", type=int, default=None)
    logs_parser.add_argument(
        "--step",
        type=int,
        default=None,
        help="Print only this step's captured log (step number from `show`)",
    )
    logs_parser.add_argument(
        "--failed-steps",
        action="store_true",
        help="Print the captured log of every step that did not pass",
    )
    logs_parser.add_argument("--output", "-o", default=None)

    # runs
    runs_parser = subparsers.add_parser("runs", help="List runs")
    runs_parser.add_argument("--repo", "-r", required=True)
    runs_parser.add_argument("--workflow", "-w", default=None)
    runs_parser.add_argument("--branch", "-b", default=None)
    runs_parser.add_argument("--status", "-s", default=None)
    runs_parser.add_argument("--limit", "-n", type=int, default=10)

    # latest
    latest_parser = subparsers.add_parser(
        "latest",
        help="Get the latest CI status of a repository",
    )
    latest_parser.add_argument("--repo", "-r", required=True)
    latest_parser.add_argument(
        "--branch",
        "-b",
        default=None,
        help="Branch to check (default: any branch; the newest run overall)",
    )
    latest_parser.add_argument("--workflow", "-w", default=None)
    latest_parser.add_argument(
        "--wait",
        type=int,
        default=None,
        help="Wait up to N seconds for the run to complete",
    )

    # pr
    pr_parser = subparsers.add_parser(
        "pr",
        help="List the CI runs of a pull request (open pull requests are the supported case)",
    )
    pr_parser.add_argument("--repo", "-r", required=True)
    pr_parser.add_argument("--pr", required=True, type=int)

    # refresh
    refresh_parser = subparsers.add_parser(
        "refresh",
        help="Force a sync of one run from GitHub (recover a missed webhook)",
    )
    refresh_parser.add_argument("--repo", "-r", required=True)
    refresh_parser.add_argument("--run-id", required=True, type=int)

    # credential: namespaced by provider kind, so a second provider adds a
    # subcommand rather than a parallel command tree.
    credential_parser = subparsers.add_parser(
        "credential", help="Credential management (operator surface)"
    )
    credential_kinds = credential_parser.add_subparsers(dest="credential_kind")
    pat_parser = credential_kinds.add_parser("pat", help="Fine-grained PAT provider")
    pat_sub = pat_parser.add_subparsers(dest="credential_command")

    pat_add = pat_sub.add_parser("add")
    pat_add.add_argument("--name", required=True)
    pat_add.add_argument("--token", required=True)
    pat_add.add_argument("--scope", default="repo,workflow")
    pat_add.add_argument(
        "--probe-repo",
        default=None,
        help=(
            "Repository used to test Actions write permission, as 'owner/repo'. "
            "The answer is repository-scoped, so without one the capability "
            "stays unknown."
        ),
    )

    pat_sub.add_parser("list")

    # ``ensure`` is the idempotent form the container entrypoint uses: create
    # when absent, rotate when present, always verifying with GitHub first.
    pat_ensure = pat_sub.add_parser("ensure")
    pat_ensure.add_argument("--name", required=True)
    pat_token = pat_ensure.add_mutually_exclusive_group(required=True)
    pat_token.add_argument("--token")
    pat_token.add_argument("--token-file")
    pat_token.add_argument("--token-stdin", action="store_true")
    pat_ensure.add_argument("--scope", default="repo,workflow")
    pat_ensure.add_argument(
        "--probe-repo",
        default=None,
        help="Repository used to test Actions write permission, as 'owner/repo'",
    )

    pat_show = pat_sub.add_parser("show")
    pat_show.add_argument("--name", required=True)
    pat_show.add_argument(
        "--probe-repo",
        default=None,
        help="Re-test Actions write permission against this repository, as 'owner/repo'",
    )

    pat_rotate = pat_sub.add_parser("rotate")
    pat_rotate.add_argument("--name", required=True)
    pat_rotate.add_argument("--token", required=True)
    pat_rotate.add_argument(
        "--probe-repo",
        default=None,
        help="Repository used to re-test Actions write permission, as 'owner/repo'",
    )

    pat_remove = pat_sub.add_parser("remove")
    pat_remove.add_argument("--name", required=True)

    # repo
    repo_parser = subparsers.add_parser("repo", help="Repository management")
    repo_subparsers = repo_parser.add_subparsers(dest="repo_command")
    repo_subscribe = repo_subparsers.add_parser("subscribe")
    repo_subscribe.add_argument("--name", required=True)
    repo_subscribe.add_argument(
        "--credential",
        default=None,
        help="Bind this credential, as 'kind/name' (omit for anonymous)",
    )
    repo_unsubscribe = repo_subparsers.add_parser("unsubscribe")
    repo_unsubscribe.add_argument("--name", required=True)
    repo_bind = repo_subparsers.add_parser("bind")
    repo_bind.add_argument("--name", required=True)
    repo_bind.add_argument("--credential", required=True, help="'kind/name'")
    repo_unbind = repo_subparsers.add_parser("unbind")
    repo_unbind.add_argument("--name", required=True)
    repo_subparsers.add_parser("list")
    repo_subparsers.add_parser("discover")

    # webhook
    webhook_parser = subparsers.add_parser("webhook", help="Webhook management")
    webhook_subparsers = webhook_parser.add_subparsers(dest="webhook_command")
    webhook_create = webhook_subparsers.add_parser("create")
    webhook_create.add_argument("--name", required=True)
    webhook_delete = webhook_subparsers.add_parser("delete")
    webhook_delete.add_argument("--name", required=True)
    webhook_list = webhook_subparsers.add_parser("list")
    webhook_list.add_argument("--name", required=True)
    webhook_ping = webhook_subparsers.add_parser("ping")
    webhook_ping.add_argument("--name", required=True)

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start services")
    serve_parser.add_argument(
        "service",
        choices=["rest", "mcp", "webhook", "all"],
        help="Service to start",
    )
    serve_parser.add_argument("--host", default=None, help="Bind host (default from config)")
    serve_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port (default from config: CITRACKER_*_PORT)",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    """CLI entry point.

    Args:
        argv: Command-line arguments (defaults to sys.argv).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    # The API key guards the protocol endpoints (REST/MCP); no CLI command
    # authenticates with it. Local operation is authorised by database access,
    # and each served surface decides for itself whether to warn.
    api_key = args.api_key or ""

    if args.command == "serve":
        # serve blocks for the lifetime of the server and manages its own loop
        serve.run(args, api_key)
        return

    runner = RUNNERS.get(args.command)
    if runner is None:  # pragma: no cover - argparse rejects unknown commands
        parser.print_help()
        sys.exit(1)
    _run_with_db(runner, args)


if __name__ == "__main__":
    main()
