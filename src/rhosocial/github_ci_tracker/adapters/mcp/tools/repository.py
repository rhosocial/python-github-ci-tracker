# src/rhosocial/github_ci_tracker/adapters/mcp/tools/repository.py
"""Repository MCP tools.

One tool, not three. ``list_repositories``, ``list_workflows`` and
``get_repository_info`` all answer parts of the same question -- "what can I
ask about?" -- and splitting them made the model pick between near-synonyms.
:func:`list_repos` takes an optional ``repository``: omitted it lists the
subscriptions, given it returns that repository's workflows and metadata.
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from ....views import as_utc
from ..result import READ_ONLY, error_envelope, paged, with_hint


def _repository_entry(repo: Any) -> dict[str, Any]:
    """Compact repository payload."""
    return {
        "full_name": repo.full_name,
        "private": repo.is_private,
        "default_branch": repo.default_branch,
        # UTC with an explicit offset, like every timestamp this service serves:
        # the row comes back in the database session's zone (+08:00 here).
        "subscribed_at": as_utc(repo.created_at).isoformat() if repo.created_at else None,
    }


def _workflow_entry(workflow: Any) -> dict[str, Any]:
    """Compact workflow payload; ``ref`` is what the other tools accept."""
    return {
        "ref": workflow.path,
        "path": workflow.path,
        "name": workflow.name,
        "workflow_id": workflow.workflow_id,
        "state": workflow.state,
    }


def register_repository_tools(server: MCPServer, container: Any) -> None:
    """Register the repository MCP tool."""

    @server.tool(
        name="list_repos",
        description=(
            "List the repositories this service tracks. Pass a repository to get "
            "that one's details and its workflows (name, path, state) instead of "
            "the whole list. Start here when you do not know what the user has "
            "authorized; the tool only reports what is already subscribed and "
            "never adds anything."
        ),
        annotations=READ_ONLY,
    )
    async def list_repos(
        repository: str | None = None,
        limit: int = 50,
        offset: int = 0,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """List tracked repositories, or describe one of them.

        Args:
            repository: ``owner/repo``; omit to list every subscription.
            limit: Page size for the list form (clamped to 50).
            offset: Rows to skip for the list form.
            refresh: Re-read this repository's workflow list from GitHub.

        Returns:
            Either ``{"items": [...]}`` or
            ``{"repository": {...}, "workflows": [...]}``. The list form does not
            carry ``workflows`` at all -- the key is absent rather than ``[]``,
            because "not read for this entry" and "this repository has none" are
            different answers and an empty list cannot express both.
        """
        size = max(1, min(limit, 50))
        page_offset = max(0, offset)

        try:
            service = container.repository

            if repository is None:
                repos = await service.list_repositories()
                page = paged(
                    [_repository_entry(repo) for repo in repos[page_offset : page_offset + size]],
                    limit=size,
                    offset=page_offset,
                    total=len(repos),
                    hint=(
                        "Pass repository=<owner/repo> to see its workflows. "
                        "Use find_runs to query runs."
                    ),
                )
                return page

            repo = await service.get_repository_info(repository)
            workflows = (
                await service.refresh_workflows(repository)
                if refresh
                else await service.list_workflows(repository)
            )
            return with_hint(
                {
                    "repository": {
                        "full_name": repo["full_name"],
                        "owner": repo["owner"],
                        "name": repo["name"],
                        "private": repo["is_private"],
                        "default_branch": repo["default_branch"],
                        "subscribed_at": (
                            as_utc(repo["subscribed_at"]).isoformat()
                            if repo.get("subscribed_at")
                            else None
                        ),
                    },
                    "workflows": [_workflow_entry(workflow) for workflow in workflows],
                },
                (
                    "Use a workflow's path (not its name) as the 'workflow' argument "
                    "of find_runs or trigger_workflow: the same workflow can be named "
                    "differently on different branches."
                    if workflows
                    else (
                        "GitHub reports no workflows for this repository, or the bound "
                        "credential cannot see them. Check the credential with ci_status, "
                        "then call again with refresh=true."
                    )
                ),
            )

        except Exception as exc:  # noqa: BLE001 - shaped for the caller
            raise error_envelope(exc) from exc
