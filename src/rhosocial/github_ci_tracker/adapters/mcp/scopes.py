# src/rhosocial/github_ci_tracker/adapters/mcp/scopes.py
"""MCP scope definitions for CI Tracker.

Scopes map one-to-one onto the read/write split the tools already honour, so an
OAuth grant can express the same limit the tools enforce. Keeping the write
scope small matters more here than elsewhere: ``citracker:write`` is the
permission to spend CI minutes and possibly deploy.
"""

from __future__ import annotations

#: Scope vocabulary.
SCOPES: list[str] = [
    # Reading runs, jobs, steps, logs and repository metadata.
    "citracker:read",
    # Triggering and re-running workflows -- spends CI minutes.
    "citracker:write",
    # Operator-only surface (credentials, repository subscriptions).
    "citracker:admin",
]

#: Tool to scope mapping. Every registered tool must appear here; a tool missing
#: from this table would be reachable without a scope check.
TOOL_SCOPES: dict[str, str] = {
    # Read-only surface.
    "ci_status": "citracker:read",
    "list_repos": "citracker:read",
    "find_runs": "citracker:read",
    "recent_runs": "citracker:read",
    "get_run": "citracker:read",
    "get_job": "citracker:read",
    "get_job_log": "citracker:read",
    "search_job_log": "citracker:read",
    "wait_for_run": "citracker:read",
    # Write surface.
    "trigger_workflow": "citracker:write",
    "cancel_run": "citracker:write",
    "rerun_run": "citracker:write",
}

#: Credential and subscription management stay out of MCP entirely; they live in
#: the CLI and the REST API. This tuple documents that decision for readers who
#: expect to find them here.
OPERATOR_ONLY: tuple[str, ...] = (
    "credentials",
    "repository_subscriptions",
)
