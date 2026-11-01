# tests/adapters/test_wait_budget.py
"""A wait budget should come from what this workflow actually took.

``wait_for_run`` asks the caller for a timeout, and a guessed timeout is wrong in
both directions: too short turns one answer into several round trips, too long
blocks for longer than the run needs. The evidence is already in the database --
this workflow's finished runs -- so it is handed over where the decision is made:

* ``trigger_workflow`` reports it, so the first wait can be budgeted in one call;
* a timed-out wait reports it as ``observed`` plus a concrete
  ``suggested_timeout_seconds``.

A fresh workflow has no history, and then both are ``null``. That is the honest
answer: an invented number reads as evidence.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from rhosocial.github_ci_tracker.adapters.mcp.tools import query as query_tools
from rhosocial.github_ci_tracker.adapters.mcp.tools import trigger as trigger_tools
from rhosocial.github_ci_tracker.views import DurationSample, RunView, RunWaitView

SAMPLE = DurationSample(samples=5, p50_seconds=120.0, p90_seconds=240.0)


class _Recorder:
    """The one method a tool group needs from the MCP server."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, *, name: str, **_: object):
        def decorate(function):
            self.registered[name] = function
            return function

        return decorate


def _tools(container: SimpleNamespace) -> dict[str, Any]:
    recorder = _Recorder()
    query_tools.register_query_tools(recorder, container)
    trigger_tools.register_trigger_tools(recorder, container)
    return recorder.registered


class _Trigger:
    def __init__(self, *, wait: RunWaitView | None = None, run: RunView | None = None) -> None:
        self._wait = wait
        self._run = run

    async def wait_for_completion(self, repository: str, run_id: int, **_kwargs: Any) -> Any:
        return self._wait

    async def trigger_workflow(self, **_: Any) -> Any:
        return self._run


def _timeout_view(sample: DurationSample | None) -> RunWaitView:
    return RunWaitView(
        run_id=90_001,
        status="in_progress",
        completed=False,
        message="still running",
        duration_sample=sample,
    )


def test_a_timeout_suggests_a_budget_from_the_workflows_p90() -> None:
    tools = _tools(SimpleNamespace(query=None, trigger=_Trigger(wait=_timeout_view(SAMPLE))))

    payload = asyncio.run(tools["wait_for_run"](repository="acme/demo", run_id=90_001))

    assert payload["timed_out"] is True
    assert payload["observed"] == {"samples": 5, "p50_seconds": 120.0, "p90_seconds": 240.0}
    assert payload["suggested_timeout_seconds"] == 240
    assert "p90 240s" in payload["hint"]


def test_a_fresh_workflow_gets_no_invented_budget() -> None:
    """Nothing has finished yet: say so instead of guessing a number."""
    tools = _tools(SimpleNamespace(query=None, trigger=_Trigger(wait=_timeout_view(None))))

    payload = asyncio.run(tools["wait_for_run"](repository="acme/fresh", run_id=90_002))

    assert payload["observed"] is None
    assert payload["suggested_timeout_seconds"] is None
    assert "No finished run" in payload["hint"]


def test_the_suggestion_is_capped_and_says_so() -> None:
    """A p90 above the cap needs more than one more call, and the hint says it."""
    huge = DurationSample(samples=3, p50_seconds=600.0, p90_seconds=3_600.0)
    tools = _tools(SimpleNamespace(query=None, trigger=_Trigger(wait=_timeout_view(huge))))

    payload = asyncio.run(tools["wait_for_run"](repository="acme/demo", run_id=90_003))

    assert payload["suggested_timeout_seconds"] == query_tools.MAX_WAIT_SECONDS
    assert "more than one wait" in payload["hint"]


def test_a_completed_wait_carries_no_budget_advice() -> None:
    """The run's own duration answers it; the controller samples only on timeout."""
    view = RunWaitView(run_id=90_004, status="completed", conclusion="success", completed=True)
    tools = _tools(SimpleNamespace(query=None, trigger=_Trigger(wait=view)))

    payload = asyncio.run(tools["wait_for_run"](repository="acme/demo", run_id=90_004))

    assert payload["completed"] is True
    assert payload["observed"] is None
    assert "suggested_timeout_seconds" not in payload


def test_a_trigger_hands_over_the_estimate_for_the_first_wait() -> None:
    run = RunView(run_id=90_005, status="queued", attempt=1, duration_sample=SAMPLE)
    tools = _tools(SimpleNamespace(query=None, trigger=_Trigger(run=run)))

    payload = asyncio.run(
        tools["trigger_workflow"](repository="acme/demo", workflow="ci.yml", ref="main")
    )

    assert payload["typical_duration_seconds"]["p90_seconds"] == 240.0
    assert payload["typical_duration_seconds"]["samples"] == 5


def test_a_trigger_on_a_fresh_workflow_reports_null() -> None:
    run = RunView(run_id=90_006, status="queued", attempt=1)
    tools = _tools(SimpleNamespace(query=None, trigger=_Trigger(run=run)))

    payload = asyncio.run(
        tools["trigger_workflow"](repository="acme/fresh", workflow="ci.yml", ref="main")
    )

    assert payload["typical_duration_seconds"] is None
