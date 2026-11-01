# tests/adapters/test_mcp_tool_wiring.py
"""Two query tools used to call methods no controller has.

``find_runs(pull_request=...)`` read ``container.query.pull_request_runs`` while
the PR lookup lives on the repository controller, and ``search_job_log`` read
``container.query.search_job_logs`` -- a method that disappeared when the old
service module was replaced. Both raised ``AttributeError`` inside the error
envelope, so a caller saw an error it could not act on and neither the unit
suite (which tested the pure ``search_log`` helper) nor the skipped integration
suite noticed.

These tests drive the two tools the way the MCP server does: register them
against a recorder, hand them a container of stubs, and call.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from rhosocial.github_ci_tracker.adapters.mcp.tools import query as query_tools
from rhosocial.github_ci_tracker.views import JobLogsView, RunRecord


class _Recorder:
    """The one method a tool group needs from the MCP server."""

    def __init__(self) -> None:
        self.registered: dict[str, object] = {}

    def tool(self, *, name: str, **_: object):
        def decorate(function):
            self.registered[name] = function
            return function

        return decorate


def _tools(container: SimpleNamespace) -> dict[str, object]:
    recorder = _Recorder()
    query_tools.register_query_tools(recorder, container)
    return recorder.registered


def _run_record() -> RunRecord:
    return RunRecord(
        run_id=90_001,
        run_number=4,
        attempt=1,
        status="completed",
        conclusion="success",
        branch="main",
        commit_sha="c" * 40,
        html_url="https://example.invalid/runs/90001",
    )


def test_find_runs_reads_a_pull_request_through_the_repository_controller() -> None:
    seen: list[tuple[str, int]] = []

    class _Repositories:
        async def pull_request_runs(self, repository: str, number: int):
            seen.append((repository, number))
            return [_run_record()]

    tools = _tools(SimpleNamespace(query=SimpleNamespace(), repository=_Repositories()))

    payload = asyncio.run(tools["find_runs"](repository="acme/demo", pull_request=7))

    assert seen == [("acme/demo", 7)]
    assert [item["run_id"] for item in payload["items"]] == [90_001]


def test_search_job_log_searches_the_log_the_query_controller_returns() -> None:
    asked: list[tuple[str, int]] = []

    class _Query:
        async def get_job_logs(self, repository: str, job_id: int, **kwargs):
            # ``step`` is part of the call now; the merged path passes None.
            assert kwargs.get("step") is None
            asked.append((repository, job_id))
            return JobLogsView(
                job_id=job_id,
                job_name="tests",
                log_content="setup\nModuleNotFoundError: x\ndone\n",
            )

    tools = _tools(SimpleNamespace(query=_Query()))

    payload = asyncio.run(
        tools["search_job_log"](repository="acme/demo", job_id=7, query="ModuleNotFound")
    )

    assert asked == [("acme/demo", 7)]
    assert payload["matches"] == [2]
    assert "ModuleNotFoundError" in payload["content"]
