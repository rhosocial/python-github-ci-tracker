# src/rhosocial/github_ci_tracker/adapters/mcp/resources/run_status.py
"""Run status MCP resources.

Resources are a *supplement* to the tools, not a replacement: client support for
subscription notifications is uneven, so nothing here is required to answer a
question. They exist for clients that can push a change notification, which is
cheaper than a model calling ``get_run`` again.

Reads are deliberately local-only. A resource read must be cheap and repeatable
-- a client may poll it -- so these never reach GitHub; the tools are where a
refresh belongs.
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer


def register_run_status_resources(server: MCPServer, container: Any) -> None:
    """Register the run status MCP resources."""

    @server.resource(uri="citracker://runs/{run_id}/status")
    async def get_run_status_resource(run_id: str) -> dict[str, Any]:
        """Report a run's state, read from GitHub.

        Args:
            run_id: GitHub run id.

        Returns:
            The stored status, or an error object when the run is unknown.
        """
        try:
            run = await container.query.get_run_status_by_id(int(run_id))
        except Exception as exc:  # noqa: BLE001 - resources report, never raise
            return {"run_id": int(run_id), "error": f"{exc.__class__.__name__}: {exc}"}

        return {
            "run_id": run.run_id,
            "status": run.status,
            "conclusion": run.conclusion,
            "branch": run.branch,
            "commit_sha": run.commit_sha,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "hint": (
                "Read from GitHub. Call get_run for the job roster, then get_job "
                "for the steps of the job that failed."
            ),
        }

    @server.resource(uri="citracker://repositories/{repo_full_name}/runs")
    async def get_repository_runs_resource(repo_full_name: str) -> dict[str, Any]:
        """Report the most recent stored runs of a repository.

        Args:
            repo_full_name: Repository full name.

        Returns:
            The recent runs, newest first.
        """
        try:
            runs = await container.query.list_runs(repo_full_name, per_page=10)
        except Exception as exc:  # noqa: BLE001 - resources report, never raise
            return {"repository": repo_full_name, "error": f"{exc.__class__.__name__}: {exc}"}

        return {
            "repository": repo_full_name,
            "runs": [
                {
                    "run_id": run.run_id,
                    "run_number": run.run_number,
                    "status": run.status,
                    "conclusion": run.conclusion,
                    "branch": run.branch,
                    "created_at": run.created_at.isoformat() if run.created_at else None,
                }
                for run in runs
            ],
        }
