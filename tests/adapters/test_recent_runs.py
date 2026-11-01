# tests/adapters/test_recent_runs.py
"""The batch summary answers for many repositories in one call.

Three properties make it usable rather than merely convenient:

* a repository that cannot be answered -- not subscribed, no credential -- is
  reported *in place*, so one bad name does not destroy a ten-repository answer;
* the payload stays a summary. Jobs, steps and logs are what ``get_run`` is for,
  and pulling them into a batch is how a "which repo is red?" question turns
  into a context flood;
* the batch is answered by **one** call into the service, not one per
  repository. Doing it per repository cost seven queries each -- five of them to
  attach jobs nobody reads -- which is most of the eight seconds a fifteen
  repository question used to take.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rhosocial.github_ci_tracker.adapters.mcp.tools import query as query_tools
from rhosocial.github_ci_tracker.core.exceptions import RepositoryNotFoundError
from rhosocial.github_ci_tracker.views import RepositoryRuns

#: Importing the module is what registers the tool; grabbing the closure is how
#: the test calls it without spinning up a transport.
#: The query controller the registered tools serve from, set by each test.
_SERVICE: object = None


def _load_tools() -> dict[str, object]:

    class Recorder:
        def __init__(self) -> None:
            self.registered: dict[str, object] = {}

        def tool(self, *, name: str, **_: object):
            def decorate(fn):
                self.registered[name] = fn
                return fn

            return decorate

    recorder = Recorder()
    query_tools.register_query_tools(recorder, SimpleNamespace(query=_SERVICE))  # type: ignore[arg-type]
    return recorder.registered


class _Run:
    def __init__(self, **kwargs: object) -> None:
        self.run_id = kwargs.get("run_id", 1)
        self.run_number = kwargs.get("run_number", 1)
        self.attempt = 1
        self.attempts_total = 1
        self.attempts_cached = 1
        self.cache_state = "cached"
        self.status = kwargs.get("status", "completed")
        self.conclusion = kwargs.get("conclusion", "success")
        self.branch = kwargs.get("branch", "main")
        self.commit_sha = "0" * 40
        self.event = "push"
        self.actor = "someone"
        self.created_at = None
        self.started_at = None
        self.completed_at = None
        self.duration_seconds = kwargs.get("duration_seconds", 60)
        self.html_url = "https://example.invalid/run"


class _StubService:
    """Stand-in for QueryController: one repo answers, the other is not tracked."""

    def __init__(self, runs_by_repo: dict[str, list[dict]]) -> None:
        self._runs = runs_by_repo
        self.batch_calls: list[list[str]] = []

    async def get_latest_run(self, repository: str, **_: object):
        return None

    async def list_run_summaries(
        self, repositories, *, workflow=None, branch=None, per_page=3
    ) -> list[RepositoryRuns]:
        self.batch_calls.append(list(repositories))
        results: list[RepositoryRuns] = []
        for name in repositories:
            if name not in self._runs:
                results.append(
                    RepositoryRuns(
                        repository=name,
                        error=RepositoryNotFoundError(f"Repository '{name}' is not tracked"),
                    )
                )
                continue
            runs = [_Run(**run) for run in self._runs[name][:per_page]]
            results.append(RepositoryRuns(repository=name, runs=runs))
        return results


async def test_one_repository_failure_does_not_lose_the_others(monkeypatch, bound) -> None:
    global _SERVICE
    _SERVICE = _StubService({"acme/green": [{"run_id": 1, "conclusion": "success"}]})
    tool = _load_tools()["recent_runs"]

    result = await tool(repositories=["acme/green", "acme/missing"])  # type: ignore[operator]

    entries = {entry["repository"]: entry for entry in result["repositories"]}
    assert [entry["repository"] for entry in result["repositories"]] == [
        "acme/green",
        "acme/missing",
    ]
    assert entries["acme/green"]["runs"][0]["conclusion"] == "success"
    assert entries["acme/missing"]["error"]["code"] == "repository_not_subscribed"


async def test_the_payload_is_a_summary(monkeypatch, bound) -> None:
    global _SERVICE
    _SERVICE = _StubService({"acme/green": [{"run_id": 7}]})
    tool = _load_tools()["recent_runs"]
    result = await tool(repositories=["acme/green"])  # type: ignore[operator]

    run = result["repositories"][0]["runs"][0]
    assert "jobs" not in run and "steps" not in run
    assert result["limit"] == 3


async def test_the_batch_is_one_call_into_the_service(monkeypatch, bound) -> None:
    """One call, not one per repository: that is the whole point of the batch."""
    global _SERVICE
    service = _StubService({"acme/green": [{"run_id": 1}], "acme/blue": [{"run_id": 2}]})
    _SERVICE = service
    tool = _load_tools()["recent_runs"]

    await tool(repositories=["acme/green", "acme/green", " acme/green ", "acme/blue"])  # type: ignore[operator]

    assert service.batch_calls == [
        ["acme/green", "acme/blue"]
    ], "duplicates collapse, and every repository is answered in one call"

    with pytest.raises(Exception) as excinfo:
        await tool(repositories=[f"acme/r{i}" for i in range(21)])  # type: ignore[operator]
    assert "At most 20 repositories" in str(excinfo.value)


async def test_an_empty_answer_is_reported_without_a_second_call(monkeypatch, bound) -> None:
    """`returned: 0` now means GitHub reported nothing: the batch already asked."""

    class _AlwaysEmpty:
        def __init__(self) -> None:
            self.batch_calls = 0

        async def list_run_summaries(self, repositories, *, workflow=None, branch=None, per_page=3):
            self.batch_calls += 1
            return [RepositoryRuns(repository=name) for name in repositories]

    global _SERVICE
    service = _AlwaysEmpty()
    _SERVICE = service
    tool = _load_tools()["recent_runs"]

    result = await tool(repositories=["acme/none", "acme/also-none"])  # type: ignore[operator]

    assert service.batch_calls == 1, "the batch is one read, not a re-check per repository"
    assert [entry["returned"] for entry in result["repositories"]] == [0, 0]
