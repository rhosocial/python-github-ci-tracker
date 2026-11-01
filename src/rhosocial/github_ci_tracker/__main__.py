# src/rhosocial/github_ci_tracker/__main__.py
"""Module-level CLI for debugging the controllers.

It builds the same container the adapters receive and drives the controllers
without a protocol adapter, against the real database and the real GitHub API. A
failure here is a controller bug rather than an adapter bug, which is the point:
the adapters are where the shapes and the error mapping live, and this entry
point has neither.

    python -m rhosocial.github_ci_tracker token add --name main --token ghp_xxx
    python -m rhosocial.github_ci_tracker repo subscribe --name o/r
    python -m rhosocial.github_ci_tracker latest --repo o/r
    python -m rhosocial.github_ci_tracker trigger --repo o/r -w ci.yml --ref main
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from .adapters.cli.eventloop import run_coroutine


def _load_token(explicit: str | None) -> str:
    """Resolve the GitHub token from the flag, environment or local.env."""
    if explicit:
        return explicit.strip()

    env = os.getenv("GITHUB_FINE_GRAINED_TOKEN")
    if env:
        return env.strip()

    for candidate in (Path.cwd() / "local.env", Path.cwd().parent / "local.env"):
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("GITHUB_FINE_GRAINED_TOKEN="):
                    return line.split("=", 1)[1].strip()

    print("Error: no token given (use --token or set GITHUB_FINE_GRAINED_TOKEN)", file=sys.stderr)
    sys.exit(1)


def _dump(payload: Any, as_json: bool) -> None:
    """Print a payload as JSON or a one-field-per-line summary."""
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    elif hasattr(payload, "__dataclass_fields__"):
        for name in payload.__dataclass_fields__:
            print(f"  {name}: {getattr(payload, name)}")
    elif isinstance(payload, list):
        for item in payload:
            _dump(item, as_json)
            print("  ---")
    else:
        print(payload)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m rhosocial.github_ci_tracker",
        description="Exercise the core services directly (database + GitHub, no adapters).",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON")

    sub = parser.add_subparsers(dest="action", required=True)

    token = sub.add_parser("token", help="Token management")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    token_add = token_sub.add_parser("add")
    token_add.add_argument("--name", required=True)
    token_add.add_argument("--token", default=None)
    token_sub.add_parser("list")
    token_verify = token_sub.add_parser("verify")
    token_verify.add_argument("--name", required=True)
    token_delete = token_sub.add_parser("delete")
    token_delete.add_argument("--name", required=True)

    repo = sub.add_parser("repo", help="Repository management")
    repo_sub = repo.add_subparsers(dest="repo_command", required=True)
    repo_subscribe = repo_sub.add_parser("subscribe")
    repo_subscribe.add_argument("--name", required=True)
    repo_unsubscribe = repo_sub.add_parser("unsubscribe")
    repo_unsubscribe.add_argument("--name", required=True)
    repo_sub.add_parser("list")
    repo_discover = repo_sub.add_parser("discover")
    repo_discover.add_argument("--token", default=None)
    repo_workflows = repo_sub.add_parser("workflows")
    repo_workflows.add_argument("--name", required=True)

    latest = sub.add_parser("latest", help="Latest run of a repo/branch/workflow")
    latest.add_argument("--repo", "-r", required=True)
    latest.add_argument("--branch", "-b", default=None)
    latest.add_argument("--workflow", "-w", default=None)

    trigger = sub.add_parser("trigger", help="Trigger a workflow")
    trigger.add_argument("--repo", "-r", required=True)
    trigger.add_argument("--workflow", "-w", required=True)
    trigger.add_argument("--ref", required=True)
    trigger.add_argument("--input", "-i", action="append", default=[])
    trigger.add_argument("--wait", type=int, default=None)

    status = sub.add_parser("status", help="Status of one run")
    status.add_argument("--repo", "-r", required=True)
    status.add_argument("--run-id", type=int, required=True)

    refresh = sub.add_parser(
        "refresh", help="Force a sync of one run from GitHub (the pull channel)"
    )
    refresh.add_argument("--repo", "-r", required=True)
    refresh.add_argument("--run-id", type=int, required=True)

    jobs = sub.add_parser("jobs", help="Jobs of one run")
    jobs.add_argument("--repo", "-r", required=True)
    jobs.add_argument("--run-id", type=int, required=True)

    logs = sub.add_parser("logs", help="Logs of one job")
    logs.add_argument("--repo", "-r", required=True)
    logs.add_argument("--job-id", type=int, required=True)
    logs.add_argument("--tail", type=int, default=None)

    runs = sub.add_parser("runs", help="List stored runs")
    runs.add_argument("--repo", "-r", required=True)
    runs.add_argument("--branch", "-b", default=None)
    runs.add_argument("--limit", "-n", type=int, default=10)

    return parser


async def _dispatch(args: argparse.Namespace) -> None:
    """Execute the requested action through the controllers."""
    from .container import Container
    from .database import Database

    database = await Database.create()
    try:
        async with database.connection():
            await _dispatch_inner(args, args.json, Container.build(database=database))
    finally:
        await database.close()


async def _dispatch_inner(args: argparse.Namespace, as_json: bool, container: Any) -> None:
    """Execute the requested action inside a bound database scope."""

    if args.action == "token":
        service = container.token
        if args.token_command == "add":
            token = await service.add_token(name=args.name, token=_load_token(args.token))
            print(f"Stored token '{token.name}' (id={token.id}).")
        elif args.token_command == "list":
            _dump(await service.list_tokens(), as_json)
        elif args.token_command == "verify":
            _dump(await service.verify_token(name=args.name), as_json)
        elif args.token_command == "delete":
            await service.delete_token(name=args.name)
            print(f"Deleted token '{args.name}'.")

    elif args.action == "repo":
        service = container.repository
        if args.repo_command == "subscribe":
            repo = await service.subscribe_repository(full_name=args.name)
            print(f"Subscribed to '{repo.full_name}' (id={repo.id}).")
        elif args.repo_command == "unsubscribe":
            await service.unsubscribe_repository(full_name=args.name)
            print(f"Unsubscribed from '{args.name}'.")
        elif args.repo_command == "list":
            _dump([repo.full_name for repo in await service.list_repositories()], as_json)
        elif args.repo_command == "discover":
            repos = await service.discover_repositories()
            print(f"Accessible repositories: {len(repos)}")
            _dump([repo.get("full_name") for repo in repos], as_json)
        elif args.repo_command == "workflows":
            workflows = await service.list_workflows(repository=args.name)
            print(f"Workflows: {len(workflows)}")
            _dump([f"{wf.name} ({wf.path})" for wf in workflows], as_json)

    elif args.action == "latest":
        service = container.query
        record = await service.get_latest_run(
            repository=args.repo, branch=args.branch, workflow=args.workflow
        )
        if record is None:
            print("No matching run.")
            return
        print(f"Run #{record.run_number} (id={record.run_id})")
        print(f"  status:     {record.status}")
        print(f"  conclusion: {record.conclusion or '-'}")
        print(f"  branch:     {record.branch}")
        print(f"  commit:     {record.commit_sha[:8]} {record.commit_message!r}")
        print(f"  url:        {record.html_url}")

    elif args.action == "trigger":
        service = container.trigger
        inputs = dict(item.split("=", 1) for item in args.input if "=" in item)
        result = await service.trigger_workflow(
            repository=args.repo,
            workflow=args.workflow,
            ref=args.ref,
            inputs=inputs or None,
        )
        print(f"Run {result.run_id} triggered (status={result.status})")
        print(f"  url: {result.html_url}")
        if args.wait:
            final = await service.wait_for_completion(
                repository=args.repo, run_id=result.run_id, timeout=float(args.wait)
            )
            print(f"  final: {final.status} ({final.conclusion or 'in progress'})")

    elif args.action == "status":
        service = container.query
        record = await service.get_run_status(repository=args.repo, run_id=args.run_id)
        _dump(record, as_json)

    elif args.action == "refresh":
        service = container.query
        result = await service.refresh_run(repository=args.repo, run_id=args.run_id)
        if as_json:
            _dump(
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
                as_json,
            )
            return
        print(f"Run {result.run_id} (#{result.run_number}) synced from GitHub.")
        print(f"  status:     {result.previous_status or '(new)'} -> {result.status}")
        print(f"  conclusion: {result.conclusion or '-'}")
        print(f"  jobs/steps: {result.jobs_synced}/{result.steps_synced} synced")
        print(f"  url:        {result.html_url}")

    elif args.action == "jobs":
        service = container.query
        records = await service.get_run_jobs(repository=args.repo, run_id=args.run_id)
        print(f"Jobs: {len(records)}")
        _dump(records, as_json)

    elif args.action == "logs":
        service = container.query
        content = await service.get_job_logs(
            repository=args.repo, job_id=args.job_id, tail=args.tail
        )
        print(content.log_content)

    elif args.action == "runs":
        service = container.query
        records = await service.list_runs(
            repository=args.repo, branch=args.branch, per_page=args.limit
        )
        print(f"Runs: {len(records)}")
        _dump(records, as_json)


def _run(coro: Coroutine[Any, Any, None]) -> None:
    """Run a coroutine on a loop psycopg accepts on Windows."""
    run_coroutine(coro)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _run(_dispatch(args))


if __name__ == "__main__":
    main()
