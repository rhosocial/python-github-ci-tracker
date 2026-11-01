# src/rhosocial/github_ci_tracker/adapters/cli/github_debug.py
"""Module-level CLI for the GitHub client.

Debugging entry point for the ``services.github.client`` module. It talks to the GitHub
API directly with a raw token and prints what came back, which is the fastest
way to answer "does this token actually work, what does the API return".

    python -m rhosocial.github_ci_tracker.adapters.cli.github_debug user --token ghp_xxx
    python -m rhosocial.github_ci_tracker.adapters.cli.github_debug runs --token ghp_xxx --repo o/r

The token is read from ``GITHUB_FINE_GRAINED_TOKEN`` when ``--token`` is
omitted, or from a ``local.env`` file next to the project root.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .eventloop import run_coroutine


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
    """Print a payload as JSON or a compact summary."""
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
        return
    if isinstance(payload, list):
        for item in payload:
            _dump(item, as_json)
    elif isinstance(payload, dict):
        summary_keys = (
            "login",
            "full_name",
            "name",
            "id",
            "status",
            "conclusion",
            "path",
            "default_branch",
            "url",
        )
        for key in summary_keys:
            if key in payload:
                print(f"  {key}: {payload[key]}")
    else:
        print(payload)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m rhosocial.github_ci_tracker.adapters.cli.github_debug",
        description="Debug the GitHub API client directly (no database, no API key).",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub token (or set GITHUB_FINE_GRAINED_TOKEN)",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON")

    sub = parser.add_subparsers(dest="action", required=True)

    user = sub.add_parser("user", help="Show the token owner and quotas")
    user.add_argument("--rate-limit", action="store_true", help="Also show the rate limit")

    repos = sub.add_parser("repos", help="List accessible repositories of an org or user")
    repos.add_argument("owner", help="Organization or user login")

    workflows = sub.add_parser("workflows", help="List the workflows of a repository")
    workflows.add_argument("repo", help="owner/repo")

    runs = sub.add_parser("runs", help="List the runs of a repository")
    runs.add_argument("repo", help="owner/repo")
    runs.add_argument("--branch", "-b", default=None)
    runs.add_argument("--workflow", "-w", default=None)
    runs.add_argument("--limit", "-n", type=int, default=10)

    run = sub.add_parser("run", help="Show one run")
    run.add_argument("repo", help="owner/repo")
    run.add_argument("run_id", type=int)

    jobs = sub.add_parser("jobs", help="List the jobs of a run")
    jobs.add_argument("repo", help="owner/repo")
    jobs.add_argument("run_id", type=int)

    logs = sub.add_parser("logs", help="Fetch the logs of a job")
    logs.add_argument("repo", help="owner/repo")
    logs.add_argument("job_id", type=int)
    logs.add_argument("--tail", type=int, default=None)

    hooks = sub.add_parser("hooks", help="List the webhooks of a repository")
    hooks.add_argument("repo", help="owner/repo")

    return parser


async def _dispatch(args: argparse.Namespace, token: str, as_json: bool) -> None:
    """Execute the requested action against the GitHub API."""
    from .client import GithubClient

    client = GithubClient()

    if args.action == "user":
        import urllib.request

        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "citracker-debug",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            _dump(json.loads(resp.read()), as_json)
        if args.rate_limit:
            import urllib.request as urlreq

            req = urlreq.Request(
                "https://api.github.com/rate_limit",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "citracker-debug",
                },
            )
            with urlreq.urlopen(req, timeout=15) as resp:
                _dump(json.loads(resp.read())["resources"]["core"], as_json)

    elif args.action == "repos":

        repos = await _paginate_org(client, token, args.owner)
        print(f"Accessible repositories: {len(repos)}")
        _dump(repos, as_json)

    elif args.action == "workflows":
        data = await client.get_repo_workflows(token, args.repo)
        print(f"Workflows: {len(data)}")
        _dump(data, as_json)

    elif args.action == "runs":
        data = await client.list_runs(
            token, args.repo, workflow_id=args.workflow, branch=args.branch, per_page=args.limit
        )
        print(f"Runs: {len(data)}")
        _dump(data, as_json)

    elif args.action == "run":
        _dump(await client.get_run(token, args.repo, args.run_id), as_json)

    elif args.action == "jobs":
        data = await client.get_run_jobs(token, args.repo, args.run_id)
        print(f"Jobs: {len(data)}")
        _dump(data, as_json)

    elif args.action == "logs":
        content = await client.get_job_logs(token, args.repo, args.job_id)
        lines = content.splitlines()
        if args.tail:
            lines = lines[-args.tail :]
        print("\n".join(lines))

    elif args.action == "hooks":
        data = await client.list_webhooks(token, args.repo)
        print(f"Webhooks: {len(data)}")
        _dump(data, as_json)


async def _paginate_org(client: Any, token: str, owner: str) -> list[dict[str, Any]]:
    """List org repositories through the low-level request helper."""
    return await client._paginate("GET", f"/orgs/{owner}/repos", token, params={"per_page": 100})


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    run_coroutine(_dispatch(args, _load_token(args.token), args.json))


if __name__ == "__main__":
    main()
