# tests/adapters/test_find_runs_shape.py
"""An empty ``find_runs`` must answer in the same shape as a non-empty one.

``find_runs`` returned ``items`` when it had rows and ``runs`` when it did not.
A caller that learned ``items`` from the first answer read ``runs`` off the
second, got undefined, and had nothing to correct itself with: the hint only
said "call get_run". An empty result is a normal answer -- a commit that has not
been picked up yet -- so it must not silently change the key.
"""

from __future__ import annotations

from types import SimpleNamespace

from rhosocial.github_ci_tracker.adapters.mcp.tools import query as query_tools

#: The query controller the registered tools serve from, set by ``_patch``.
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


class _Item:
    run_id = 1
    run_number = 1
    attempt = 1
    attempts_total = 1
    attempts_cached = 1
    cache_state = "cached"
    status = "completed"
    conclusion = "success"
    branch = "main"
    commit_sha = "0" * 40
    event = "push"
    actor = "someone"
    created_at = None
    started_at = None
    completed_at = None
    duration_seconds = 60
    html_url = "https://example.invalid/run"


class _StubService:
    def __init__(self, runs: list[object]) -> None:
        self._runs = runs
        self.attached = 0

    async def list_runs(self, repository: str, **_: object) -> list[object]:
        return list(self._runs)

    async def attach_failed_jobs(self, runs: list[object]) -> None:
        from rhosocial.github_ci_tracker.views import JobFailure

        self.attached += len(runs)
        for run in runs:
            run.failed_jobs = [JobFailure(name="tests", conclusion="failure")]

    async def get_latest_run(self, repository: str, **_: object) -> object | None:
        return None

    async def pull_request_runs(self, repository: str, number: int, **_: object):
        return list(self._runs)


def _patch(monkeypatch, runs: list[object]) -> None:
    """Make the next registration serve from a stub controller."""
    global _SERVICE
    _SERVICE = _StubService(runs)


async def test_an_empty_result_uses_the_same_key(monkeypatch, bound) -> None:
    _patch(monkeypatch, [])

    result = await _load_tools()["find_runs"](repository="acme/empty")  # type: ignore[operator]

    assert result["items"] == []
    assert "runs" not in result
    assert "GitHub has no matching run" in result["hint"]


async def test_a_non_empty_result_names_its_key_and_pages(monkeypatch, bound) -> None:
    _patch(monkeypatch, [_Item()])

    result = await _load_tools()["find_runs"](repository="acme/demo")  # type: ignore[operator]

    assert result["items"][0]["run_id"] == 1
    assert "items" in result["hint"]


async def test_the_empty_answer_still_says_who_was_asked(monkeypatch, bound) -> None:
    _patch(monkeypatch, [])

    result = await _load_tools()["find_runs"](repository="acme/empty")  # type: ignore[operator]

    assert result["items"] == []
    assert result["repository"] == "acme/empty", "the call names who was asked"


async def test_include_jobs_failed_asks_for_the_failing_names(monkeypatch, bound) -> None:
    """One call, not one ``get_run`` per repository."""
    _patch(monkeypatch, [_Item()])

    result = await _load_tools()["find_runs"](  # type: ignore[operator]
        repository="acme/demo", include_jobs="failed"
    )

    assert result["items"][0]["failed_jobs"] == [{"name": "tests", "conclusion": "failure"}]


async def test_the_default_list_does_not_ask(monkeypatch, bound) -> None:
    _patch(monkeypatch, [_Item()])

    result = await _load_tools()["find_runs"](repository="acme/demo")  # type: ignore[operator]

    assert "failed_jobs" not in result["items"][0]
