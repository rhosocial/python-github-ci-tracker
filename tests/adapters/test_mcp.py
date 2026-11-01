# tests/adapters/test_mcp.py
"""MCP adapter tests: tool surface, log windowing and error shaping.

The tool *surface* is what these tests pin -- names, read/write annotations and
arguments -- because that is the contract a model sees. Behaviour that reaches
GitHub belongs to the core tests; here the log window and the error envelope are
exercised directly, since both are pure functions of their inputs.

No database is bound: everything under test is either a pure function or the
server's registration table.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from rhosocial.github_ci_tracker.adapters.mcp.logs import (
    DEFAULT_TAIL,
    MAX_CHARS,
    window_log,
)
from rhosocial.github_ci_tracker.adapters.mcp.result import (
    MCPToolError,
    error_envelope,
    error_result,
    origin,
    paged,
    set_request_origin,
    settings_url,
)
from rhosocial.github_ci_tracker.adapters.mcp.scopes import TOOL_SCOPES
from rhosocial.github_ci_tracker.adapters.mcp.server import INSTRUCTIONS, create_mcp_server
from rhosocial.github_ci_tracker.core.exceptions import (
    RepositoryNotFoundError,
    RunNotFoundError,
    TokenExpiredError,
    TriggerFailedError,
    WorkflowNotFoundError,
)

#: The tool surface, in the order a model encounters it.
EXPECTED_TOOLS = {
    "ci_status": True,
    "list_repos": True,
    "find_runs": True,
    "recent_runs": True,
    "get_run": True,
    "get_job": True,
    "get_job_log": True,
    "search_job_log": True,
    "wait_for_run": True,
    "trigger_workflow": False,
    "rerun_run": False,
    "cancel_run": False,
}


def _tools() -> dict[str, object]:
    """Registered tools, keyed by name."""

    async def collect() -> dict[str, object]:
        server = create_mcp_server()
        return {tool.name: tool for tool in await server.list_tools()}

    return asyncio.run(collect())


def test_tool_surface_is_small_and_annotated() -> None:
    """Eight tools, each declaring whether it only reads."""
    tools = _tools()
    assert set(tools) == set(EXPECTED_TOOLS), "tool surface changed"

    for name, read_only in EXPECTED_TOOLS.items():
        annotation = tools[name].annotations
        assert annotation is not None, f"{name} has no annotations"
        assert annotation.read_only_hint is read_only, f"{name} read_only_hint"

    # Triggering must never claim idempotence: a retry starts a second run.
    for name in ("trigger_workflow", "rerun_run"):
        assert tools[name].annotations.idempotent_hint is False


def test_every_tool_has_a_scope() -> None:
    """A tool missing from TOOL_SCOPES would bypass the scope check."""
    assert set(_tools()) == set(TOOL_SCOPES)


def test_retired_tool_names_are_gone() -> None:
    """The merged-away tools must not come back as duplicates."""
    retired = {
        "get_run_status",
        "list_runs",
        "get_latest_run",
        "refresh_run",
        "get_pull_request_runs",
        "get_run_jobs",
        "get_job_logs",
        "list_repositories",
        "list_workflows",
        "get_repository_info",
        "rerun_workflow",
    }
    assert retired.isdisjoint(_tools())


def test_instructions_are_short_and_carry_the_two_invariants() -> None:
    """The instructions must stay short and warn about logs and retries."""
    assert len(INSTRUCTIONS) < 1200
    lowered = INSTRUCTIONS.lower()
    assert "untrusted" in lowered
    assert "not idempotent" in lowered
    assert "never ask for or accept tokens" in lowered


def test_log_window_defaults_to_tail() -> None:
    content = "\n".join(f"line {i}" for i in range(1, 501))
    window = window_log(content, tail=DEFAULT_TAIL)
    assert window.total_lines == 500
    assert window.first_line == 500 - DEFAULT_TAIL + 1
    assert window.last_line == 500
    assert window.truncated is True


def test_log_window_explicit_range() -> None:
    content = "\n".join(f"line {i}" for i in range(1, 21))
    window = window_log(content, from_line=5, to_line=7)
    assert window.content.splitlines() == ["line 5", "line 6", "line 7"]
    assert (window.first_line, window.last_line) == (5, 7)
    assert window.truncated is True


def test_log_window_grep_keeps_context_and_reports_matches() -> None:
    lines = [f"line {i}" for i in range(1, 21)]
    lines[9] = "boom: something failed"
    window = window_log("\n".join(lines), grep="boom", context=1)
    assert window.matches == [10]
    assert window.content.splitlines() == ["line 9", "boom: something failed", "line 11"]


def test_log_window_grep_without_match_returns_empty() -> None:
    window = window_log("a\nb\nc", grep="zzz")
    assert window.matches == []
    assert window.content == ""


def test_log_window_invalid_regex_falls_back_to_literal() -> None:
    """A malformed pattern must still search, not raise."""
    window = window_log("value is a(b here", grep="a(b")
    assert window.matches == [1]


def test_log_window_truncates_by_characters() -> None:
    window = window_log("x" * (MAX_CHARS + 500), tail=10_000)
    assert len(window.content) == MAX_CHARS
    assert window.truncated is True


def test_log_window_empty_document() -> None:
    window = window_log("")
    assert window.total_lines == 0
    assert window.content == ""
    assert window.truncated is False


def test_error_result_points_at_settings_for_subscription() -> None:
    payload = error_result(RepositoryNotFoundError("not tracked"))
    assert payload["isError"] is True
    assert payload["error"]["code"] == "repository_not_subscribed"
    assert "token" not in json.dumps(payload).lower().replace("token.", "")


def test_action_url_is_absolute_or_absent(monkeypatch) -> None:
    """A settings path is only published when it can actually be opened.

    A relative path is worse than none: the instructions tell a model to hand
    the user the URL from the error, and a URL without a scheme cannot be
    opened.
    """
    monkeypatch.delenv("CITRACKER_PUBLIC_BASE_URL", raising=False)
    set_request_origin("")

    assert error_result(RepositoryNotFoundError("x"))["error"].get("action_url") == ""
    assert settings_url() == ""

    set_request_origin("https", "citracker.example.com")
    assert error_result(RepositoryNotFoundError("x"))["error"]["action_url"] == (
        "https://citracker.example.com/api/v1/repositories"
    )
    assert settings_url() == "https://citracker.example.com/api/v1/repositories"

    # An explicit configuration beats whatever the caller put in its Host.
    monkeypatch.setenv("CITRACKER_PUBLIC_BASE_URL", "https://ops.internal:8443/")
    assert origin() == "https://ops.internal:8443"
    assert settings_url() == "https://ops.internal:8443/api/v1/repositories"


def test_error_envelope_raises_for_the_transport() -> None:
    """A failure must raise, so the MCP layer reports ``isError: true``.

    Returning the envelope as the result text is what made a failed call look
    successful to a client that only inspects the protocol status.
    """
    error = error_envelope(RepositoryNotFoundError("x"))
    assert isinstance(error, MCPToolError)

    payload = json.loads(str(error))
    assert payload["error"]["code"] == "repository_not_subscribed"
    assert error.payload == payload

    # Already-shaped errors are re-raised, not wrapped twice.
    assert error_envelope(error) is error


def test_error_result_never_asks_for_a_token() -> None:
    """Every credential error must send the user to settings, not to a token."""
    set_request_origin("https", "citracker.example.com")
    for exc in (
        TokenExpiredError("expired", token_name="work"),
        RepositoryNotFoundError("nope"),
    ):
        payload = error_result(exc)
        hint = payload["error"]["hint"].lower()
        assert "ask the user" in hint
        # The fix is always an operator action in settings; the caller must
        # never be told to go find a credential itself.
        assert payload["error"]["action_url"]
        assert "provide a token" not in hint
        assert "paste" not in hint
    set_request_origin("")


def test_error_result_codes_for_run_and_workflow() -> None:
    assert error_result(RunNotFoundError("x"))["error"]["code"] == "run_not_found"
    assert error_result(WorkflowNotFoundError("x"))["error"]["code"] == "workflow_not_found"


def test_error_result_wraps_unexpected_exceptions() -> None:
    payload = error_result(ValueError("boom"))
    assert payload["error"]["code"] == "internal_error"
    assert "ValueError" in payload["error"]["message"]


def test_a_workflow_without_workflow_dispatch_is_not_a_lookup_failure() -> None:
    """The workflow resolved; GitHub refused it for a different reason.

    Reporting this as ``trigger_failed`` with "check find_runs before retrying"
    made the caller re-read the name, because the previous attempt had failed on
    the name. The two refusals must be distinguishable.
    """
    payload = error_result(
        TriggerFailedError("Workflow does not have 'workflow_dispatch' trigger", 422, "")
    )

    assert payload["error"]["code"] == "workflow_not_dispatchable"
    assert "workflow_dispatch" in payload["error"]["hint"]
    assert payload["error"]["code"] != "workflow_not_found"


def test_paged_marks_truncation_and_next_offset() -> None:
    page = paged([1, 2, 3], limit=3, offset=0)
    assert page["truncated"] is True
    assert page["next_offset"] == 3
    assert "hint" in page


def test_paged_complete_page_is_not_truncated() -> None:
    page = paged([1, 2], limit=10, offset=0, total=2)
    assert page["truncated"] is False
    assert page["total"] == 2
    assert "next_offset" not in page


def _history_service(history):
    """A container whose query controller answers one history."""

    class _Stub:
        async def job_failure_history(self, repository, job_name, sample=10):  # noqa: ANN001
            return history

    return SimpleNamespace(query=_Stub())


def test_the_job_history_renders_the_verdict_the_service_decided(monkeypatch) -> None:
    """The counts and verdict are the service's; only the sentence is written here."""
    from rhosocial.github_ci_tracker.adapters.mcp.tools import query as query_tools
    from rhosocial.github_ci_tracker.views import JobHistoryView

    history = JobHistoryView(job_name="build", sampled_executions=3, failures=3)

    rendered = asyncio.run(
        query_tools._recent_failure_ratio(  # noqa: SLF001
            _history_service(history), "acme/demo", "build", 3
        )
    )

    assert rendered is not None
    assert (rendered["sampled_executions"], rendered["failures"]) == (3, 3)
    assert "failed in every recent execution" in rendered["hint"]


def test_an_intermittent_job_is_described_by_its_counts(monkeypatch) -> None:
    from rhosocial.github_ci_tracker.adapters.mcp.tools import query as query_tools
    from rhosocial.github_ci_tracker.views import JobHistoryView

    history = JobHistoryView(job_name="build", sampled_executions=10, failures=2)

    rendered = asyncio.run(
        query_tools._recent_failure_ratio(  # noqa: SLF001
            _history_service(history), "acme/demo", "build"
        )
    )

    assert rendered is not None
    assert rendered["hint"] == "This job failed in 2 of 10 recent executions."


def test_no_history_is_reported_as_no_history(monkeypatch) -> None:
    from rhosocial.github_ci_tracker.adapters.mcp.tools import query as query_tools

    assert (
        asyncio.run(
            query_tools._recent_failure_ratio(  # noqa: SLF001
                _history_service(None), "acme/demo", "build"
            )
        )
        is None
    )


def test_the_trigger_scope_lists_the_workflows_it_was_given() -> None:
    """The scope is a rendering step: the workflows come from the service."""
    from rhosocial.github_ci_tracker.adapters.mcp.tools.status import _trigger_scope

    workflows = {"acme/demo": [SimpleNamespace(path=".github/workflows/ci.yml", name="CI")]}

    scope = _trigger_scope(["acme/demo", "acme/none"], workflows, writable=False)

    assert [entry["repository"] for entry in scope] == ["acme/demo", "acme/none"]
    assert scope[0]["workflows"] == [{"path": ".github/workflows/ci.yml", "name": "CI"}]
    assert scope[0]["trigger_allowed"] is False
    assert scope[0]["reason"] is not None
    assert scope[1]["workflows"] == []


def test_ci_status_reports_what_the_services_say(monkeypatch) -> None:
    """The payload is assembled from service answers, never from a query."""
    from rhosocial.github_ci_tracker.adapters.mcp.tools import status as status_tools
    from rhosocial.github_ci_tracker.views import BindingSummary, StatusSnapshot, StreamHealth

    repo = SimpleNamespace(full_name="acme/demo", is_private=False, default_branch="main")
    workflow = SimpleNamespace(path=".github/workflows/ci.yml", name="CI")

    class _Repositories:
        async def list_repositories(self, filter=None):  # noqa: ANN001, ANN202
            return [repo]

        async def binding_summary(self):  # noqa: ANN202
            return BindingSummary(bound=1, anonymous=0)

        async def workflows_of(self, names):  # noqa: ANN001, ANN202
            return {"acme/demo": [workflow]}

    class _Tokens:
        async def list_tokens(self, limit=100, offset=0):  # noqa: ANN001, ANN202
            return [
                {
                    "credential": "pat/work",
                    "owner_login": "alice",
                    "is_active": True,
                    "expires_at": None,
                    "can_write_actions": True,
                }
            ]

    class _Status:
        async def snapshot(self):  # noqa: ANN202
            return StatusSnapshot(
                repository_count=1,
                runs_tracked=2,
                streams={"workflow_run": StreamHealth(event_type="workflow_run")},
                push_channel="unknown",
            )

    container = SimpleNamespace(repository=_Repositories(), token=_Tokens(), status=_Status())

    # The registry decorator is the only thing the tool needs from the server.
    class _Recorder:
        def __init__(self) -> None:
            self.registered: dict[str, object] = {}

        def tool(self, *, name: str, **_: object):
            def decorate(function):
                self.registered[name] = function
                return function

            return decorate

    recorder = _Recorder()
    status_tools.register_status_tools(recorder, container)  # type: ignore[arg-type]
    payload = asyncio.run(
        recorder.registered["ci_status"](include_repositories=True)  # type: ignore[operator]
    )

    assert payload["capabilities"]["can_trigger"] is True
    assert payload["capabilities"]["trigger_scope"] == [
        {
            "repository": "acme/demo",
            "trigger_allowed": True,
            "reason": None,
            "workflows": [{"path": ".github/workflows/ci.yml", "name": "CI"}],
        }
    ]
    assert payload["identity"]["anonymous_subscriptions"] == 0
    assert payload["health"]["repository_count"] == 1
    assert payload["health"]["runs_tracked"] == 2
    assert payload["health"]["streams"]["workflow_run"]["last_at"] is None


def _registered(tools_module, register, monkeypatch, *, result):
    """Register a tool group against a recorder, with a stubbed service."""
    from rhosocial.github_ci_tracker.views import RunView  # noqa: F401 - documented below

    class _Tokens:
        async def list_tokens(self, limit=100, offset=0):  # noqa: ANN001, ANN202
            return [{"is_active": True}]

    class _Trigger:
        async def rerun_workflow(self, repository, run_id):  # noqa: ANN001, ANN202
            return result

        async def trigger_workflow(
            self, repository, workflow, ref, inputs=None, *, if_absent=False, commit_sha=None
        ):  # noqa: ANN001, ANN202
            return result

    container = SimpleNamespace(token=_Tokens(), trigger=_Trigger())

    class _Recorder:
        def __init__(self) -> None:
            self.registered: dict[str, object] = {}

        def tool(self, *, name: str, **_: object):
            def decorate(function):
                self.registered[name] = function
                return function

            return decorate

    recorder = _Recorder()
    register(recorder, container)
    return recorder.registered


def test_a_running_run_is_not_reported_as_a_credential_problem() -> None:
    """The 403 GitHub sends for concurrency must not send anyone to the token."""
    from rhosocial.github_ci_tracker.core.exceptions import RunInProgressError

    payload = error_result(RunInProgressError("This workflow is already running"))

    assert payload["error"]["code"] == "run_in_progress"
    assert payload["error"]["code"] != "credential_insufficient"
    assert "wait" in payload["error"]["hint"].lower()


def test_a_real_permission_error_still_says_so() -> None:
    from rhosocial.github_ci_tracker.core.exceptions import TokenPermissionError

    payload = error_result(TokenPermissionError("Insufficient token permissions"))

    assert payload["error"]["code"] == "credential_insufficient"


def test_a_rejected_state_change_is_a_conflict_not_a_permission() -> None:
    from rhosocial.github_ci_tracker.core.exceptions import GithubAPIError

    payload = error_result(GithubAPIError("conflict", status_code=409))

    assert payload["error"]["code"] == "run_state_conflict"


def test_a_rerun_reports_the_attempt_it_started(monkeypatch) -> None:
    """The caller can confirm "this is my Nth attempt" without asking GitHub."""
    from rhosocial.github_ci_tracker.adapters.mcp.tools import trigger as trigger_tools
    from rhosocial.github_ci_tracker.views import RunView

    tools = _registered(
        trigger_tools,
        trigger_tools.register_trigger_tools,
        monkeypatch,
        result=RunView(
            run_id=90_001,
            status="queued",
            attempt=2,
            previous_attempt=1,
            html_url="https://example.invalid/run",
        ),
    )

    payload = asyncio.run(tools["rerun_run"](repository="acme/demo", run_id=90_001))

    assert payload["run_attempt"] == 2
    assert payload["previous_attempt"] == 1
    assert payload["advanced"] is True
    assert "attempt 2" in payload["hint"]


def test_a_rerun_github_has_not_reflected_yet_says_so(monkeypatch) -> None:
    """A lagging read is reported, not dressed up as a new attempt."""
    from rhosocial.github_ci_tracker.adapters.mcp.tools import trigger as trigger_tools
    from rhosocial.github_ci_tracker.views import RunView

    tools = _registered(
        trigger_tools,
        trigger_tools.register_trigger_tools,
        monkeypatch,
        result=RunView(run_id=90_001, status="queued", attempt=1, previous_attempt=1),
    )

    payload = asyncio.run(tools["rerun_run"](repository="acme/demo", run_id=90_001))

    assert payload["advanced"] is False
    assert "not visible yet" in payload["hint"]


def test_a_trigger_says_which_attempt_it_created(monkeypatch) -> None:
    from rhosocial.github_ci_tracker.adapters.mcp.tools import trigger as trigger_tools
    from rhosocial.github_ci_tracker.views import RunView

    tools = _registered(
        trigger_tools,
        trigger_tools.register_trigger_tools,
        monkeypatch,
        result=RunView(run_id=90_002, status="queued", attempt=1),
    )

    payload = asyncio.run(
        tools["trigger_workflow"](repository="acme/demo", workflow="ci.yml", ref="main")
    )

    assert payload["run_attempt"] == 1
    assert payload["dispatched"] is True


def test_a_reused_run_says_nothing_was_dispatched(monkeypatch) -> None:
    """``if_absent`` spends no CI minutes, and the answer must say so."""
    from rhosocial.github_ci_tracker.adapters.mcp.tools import trigger as trigger_tools
    from rhosocial.github_ci_tracker.views import RunView

    tools = _registered(
        trigger_tools,
        trigger_tools.register_trigger_tools,
        monkeypatch,
        result=RunView(
            run_id=90_003,
            status="completed",
            attempt=1,
            dispatched=False,
            message="'ci.yml' already has a run for main at abc1234; nothing was dispatched.",
        ),
    )

    payload = asyncio.run(
        tools["trigger_workflow"](repository="acme/demo", workflow="ci.yml", ref="main")
    )

    assert payload["dispatched"] is False
    assert payload["run_id"] == 90_003
    assert "nothing was dispatched" in payload["hint"]


def test_an_unmapped_403_about_the_run_is_still_not_a_credential_problem() -> None:
    """The transport error can reach the envelope before map_api_error does."""
    from rhosocial.github_ci_tracker.core.exceptions import GithubAPIError

    payload = error_result(GithubAPIError("This workflow is already running", status_code=403))

    assert payload["error"]["code"] == "run_in_progress"


def test_run_payload_timestamps_are_utc() -> None:
    """One response must not mix +08:00 and +00:00."""
    from datetime import datetime, timedelta, timezone

    from rhosocial.github_ci_tracker.adapters.mcp.tools.query import _run_summary
    from rhosocial.github_ci_tracker.views import _to_run_record

    shanghai = timezone(timedelta(hours=8))
    run = SimpleNamespace(
        github_run_id=90_001,
        run_number=7,
        latest_attempt=1,
        latest_status="completed",
        latest_conclusion="success",
        branch="main",
        commit_sha="abc",
        commit_message="msg",
        actor="alice",
        event="push",
        pr_number=None,
        created_at=datetime(2026, 10, 10, 18, 9, tzinfo=shanghai),
        latest_github_created_at=datetime(2026, 10, 10, 17, 59, tzinfo=shanghai),
        latest_started_at=datetime(2026, 10, 10, 18, 0, tzinfo=shanghai),
        latest_completed_at=datetime(2026, 10, 10, 18, 5, tzinfo=shanghai),
    )

    payload = _run_summary(_to_run_record(run, "acme/demo"))

    assert payload["started_at"] == "2026-10-10T10:00:00+00:00"
    assert payload["completed_at"] == "2026-10-10T10:05:00+00:00"


def test_get_run_can_report_a_past_attempt(monkeypatch) -> None:
    """A past attempt is read, and its jobs are the ones stored for it."""
    from rhosocial.github_ci_tracker.adapters.mcp.tools import query as query_tools
    from rhosocial.github_ci_tracker.views import JobRecord, RunRecord

    record = RunRecord(
        run_id=90_001,
        run_number=7,
        attempt=1,
        is_latest=False,
        attempts_total=2,
        status="completed",
        conclusion="failure",
        jobs=[
            JobRecord(
                job_id=1,
                run_id=90_001,
                name="build",
                status="completed",
                conclusion="failure",
            )
        ],
    )

    class _Query:
        async def get_run_status(self, repository, run_id, attempt=None):  # noqa: ANN001, ANN202
            assert attempt == 1
            return record

        async def get_run_jobs(self, repository, run_id):  # noqa: ANN001, ANN202
            raise AssertionError("a past attempt must not fetch the latest attempt's jobs")

        async def get_job_logs(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise AssertionError("no log was asked for")

    container = SimpleNamespace(query=_Query())

    class _Recorder:
        def __init__(self) -> None:
            self.registered: dict[str, object] = {}

        def tool(self, *, name: str, **_: object):
            def decorate(function):
                self.registered[name] = function
                return function

            return decorate

    recorder = _Recorder()
    query_tools.register_query_tools(recorder, container)  # type: ignore[arg-type]
    payload = asyncio.run(
        recorder.registered["get_run"](  # type: ignore[operator]
            run_id=90_001, repository="acme/demo", attempt=1
        )
    )

    assert payload["attempt"] == 1
    assert payload["is_latest"] is False
    assert payload["conclusion"] == "failure"
    assert "attempt 1 of 2" in payload["hint"]
    assert [job["job_id"] for job in payload["jobs"]] == [1]


def test_the_attempt_hint_survives_include_jobs_false(monkeypatch) -> None:
    """Omitting jobs must not hide the fact that this is a past attempt."""
    from rhosocial.github_ci_tracker.adapters.mcp.tools import query as query_tools
    from rhosocial.github_ci_tracker.views import RunRecord

    record = RunRecord(run_id=90_001, run_number=7, attempt=1, is_latest=False, attempts_total=2)

    class _Query:
        async def get_run_status(self, repository, run_id, attempt=None):  # noqa: ANN001, ANN202
            return record

    container = SimpleNamespace(query=_Query())

    class _Recorder:
        def __init__(self) -> None:
            self.registered: dict[str, object] = {}

        def tool(self, *, name: str, **_: object):
            def decorate(function):
                self.registered[name] = function
                return function

            return decorate

    recorder = _Recorder()
    query_tools.register_query_tools(recorder, container)  # type: ignore[arg-type]
    payload = asyncio.run(
        recorder.registered["get_run"](  # type: ignore[operator]
            run_id=90_001, repository="acme/demo", attempt=1, include_jobs=False
        )
    )

    assert "attempt 1 of 2" in payload["hint"]


def test_list_repos_omits_workflows_the_list_form_never_read() -> None:
    """Absent, not empty: "not read" and "this repository has none" differ.

    The single-repository form reads the workflow list; the batch form does not.
    Answering ``[]`` there made the two indistinguishable, so the key is left
    out entirely -- and the timestamp still has to be UTC, which is the other
    thing this payload gets wrong when it renders a database row as-is.
    """
    from datetime import datetime, timedelta, timezone

    from rhosocial.github_ci_tracker.adapters.mcp.tools import repository as repo_tools

    shanghai = timezone(timedelta(hours=8))
    repo = SimpleNamespace(
        full_name="acme/demo",
        is_private=False,
        default_branch="main",
        created_at=datetime(2026, 10, 29, 0, 37, 36, tzinfo=shanghai),
    )

    class _Repositories:
        async def list_repositories(self):  # noqa: ANN202
            return [repo]

    class _Recorder:
        def __init__(self) -> None:
            self.registered: dict[str, object] = {}

        def tool(self, *, name: str, **_: object):
            def decorate(function):
                self.registered[name] = function
                return function

            return decorate

    recorder = _Recorder()
    repo_tools.register_repository_tools(
        recorder,
        SimpleNamespace(repository=_Repositories()),  # type: ignore[arg-type]
    )
    payload = asyncio.run(recorder.registered["list_repos"]())  # type: ignore[operator]

    entry = payload["items"][0]
    assert "workflows" not in entry, "the list form never read them"
    assert entry["subscribed_at"].endswith("+00:00"), "timestamps are UTC, zone stated"


def test_a_run_summary_carries_identity_and_a_bounded_subject() -> None:
    """Two runs of one sha differ by workflow; the body stays out of the list."""
    from types import SimpleNamespace

    from rhosocial.github_ci_tracker.adapters.mcp.tools.query import _run_summary

    run = SimpleNamespace(
        run_id=90_001,
        run_number=1,
        attempt=1,
        status="completed",
        conclusion="success",
        branch="main",
        commit_sha="a" * 40,
        commit_message="feat: the subject\n\nA body paragraph long enough to matter.",
        commit_author="Alice",
        commit_time=None,
        display_title="feat: the subject (#12)",
        head_repo="fork/demo",
        base_ref="main",
        pr_number=12,
        workflow_name="Test and lint",
        workflow_path=".github/workflows/test.yml",
        event="pull_request",
        actor="bob",
        created_at=None,
        started_at=None,
        completed_at=None,
        stored_at=None,
        duration_seconds=60,
        html_url="https://example.invalid/run",
    )

    payload = _run_summary(run)

    assert "failed_jobs" not in payload, "not asked for"
    assert payload["workflow"] == {
        "name": "Test and lint",
        "path": ".github/workflows/test.yml",
    }
    assert payload["commit"]["message"] == "feat: the subject"
    assert payload["commit"]["author"] == "Alice"
    assert payload["pr_number"] == 12
    assert payload["base_ref"] == "main"


def test_a_run_summary_reports_the_failing_jobs_when_asked() -> None:
    from types import SimpleNamespace

    from rhosocial.github_ci_tracker.adapters.mcp.tools.query import _run_summary
    from rhosocial.github_ci_tracker.views import JobFailure

    run = SimpleNamespace(
        run_id=90_009,
        run_number=2,
        attempt=1,
        status="completed",
        conclusion="failure",
        branch="main",
        commit_sha="b" * 40,
        commit_message="fix: x",
        event="push",
        actor="bob",
        created_at=None,
        started_at=None,
        completed_at=None,
        duration_seconds=90,
        html_url="https://example.invalid/run",
        failed_jobs=[
            JobFailure(name="tests", conclusion="failure"),
            JobFailure(name="lint", conclusion="cancelled"),
        ],
    )

    payload = _run_summary(run)

    assert payload["failed_jobs"] == [
        {"name": "tests", "conclusion": "failure"},
        {"name": "lint", "conclusion": "cancelled"},
    ]


def _log_tool(stub):
    """Register the query tools against a container with one stub service."""
    from types import SimpleNamespace

    from rhosocial.github_ci_tracker.adapters.mcp.tools import query as query_tools

    class _Recorder:
        def __init__(self) -> None:
            self.registered: dict[str, object] = {}

        def tool(self, *, name: str, **_: object):
            def decorate(function):
                self.registered[name] = function
                return function

            return decorate

    recorder = _Recorder()
    query_tools.register_query_tools(recorder, SimpleNamespace(query=stub, token=SimpleNamespace()))
    return recorder.registered


def test_get_job_log_step_returns_that_step_only() -> None:
    """Step mode publishes sections, not a window over the merged document."""
    from rhosocial.github_ci_tracker.views import JobLogsView, StepSection

    view = JobLogsView(
        job_id=7,
        job_name="build",
        source="step",
        sections=[
            StepSection(
                number=2,
                name="Run tests",
                conclusion="failure",
                content="boom\n",
                line_count=1,
            )
        ],
    )

    class _Query:
        async def get_job_logs(self, repository, job_id, **kwargs):  # noqa: ANN001, ANN003, ANN202
            assert kwargs == {"step": 2, "failed_steps_only": False}
            return view

    payload = asyncio.run(
        _log_tool(_Query())["get_job_log"](repository="acme/demo", job_id=7, step=2)
    )

    assert payload["source"] == "step"
    assert payload["step"] == 2
    assert payload["content"] == "boom\n", "one step is a document, like the merged mode"
    assert payload["step_info"] == {"name": "Run tests", "conclusion": "failure", "lines": 1}
    assert payload["total_lines"] == 1
    assert payload["steps_without_logs"] == []
    assert "Line numbers are local" in payload["hint"]


def test_get_job_log_without_step_text_says_so_instead_of_guessing() -> None:
    """No captured step is an answer; the merged document is the alternative."""
    from rhosocial.github_ci_tracker.views import JobLogsView

    view = JobLogsView(
        job_id=7,
        job_name="build",
        source="step",
        steps_without_logs=[2],
        reason="capture_closed",
    )

    class _Query:
        async def get_job_logs(self, repository, job_id, **kwargs):  # noqa: ANN001, ANN003, ANN202
            return view

    payload = asyncio.run(
        _log_tool(_Query())["get_job_log"](repository="acme/demo", job_id=7, failed_steps_only=True)
    )

    assert payload["sections"] == []
    assert payload["reason"] == "capture_closed"
    assert "about an hour" in payload["hint"]


def test_a_green_job_reports_nothing_failed_rather_than_a_missing_capture() -> None:
    """``failed_steps_only`` on a passing job is not a capture failure.

    The reason already says ``no_failed_steps``; the hint used to say the failed
    steps' text "was never fetched", which sends a reader looking for a capture
    that never needed to happen.
    """
    from rhosocial.github_ci_tracker.views import JobLogsView

    view = JobLogsView(
        job_id=7,
        job_name="transfer",
        source="step",
        steps_without_logs=[1, 2],
        reason="no_failed_steps",
    )

    class _Query:
        async def get_job_logs(self, repository, job_id, **kwargs):  # noqa: ANN001, ANN003, ANN202
            return view

    payload = asyncio.run(
        _log_tool(_Query())["get_job_log"](repository="acme/demo", job_id=7, failed_steps_only=True)
    )

    assert payload["reason"] == "no_failed_steps"
    assert "No step of this job failed" in payload["hint"]
    assert "never fetched" not in payload["hint"]


def test_get_job_log_resolves_the_repository_when_it_is_omitted() -> None:
    """Job ids are globally unique; asking for the repository again is friction.

    ``get_job`` already resolves it from the stored row, and the two tools
    describe the same job.
    """
    from rhosocial.github_ci_tracker.views import JobLogsView

    view = JobLogsView(job_id=7, job_name="build", log_content="one\n")

    class _Query:
        async def repository_name_of_job(self, job_id):  # noqa: ANN001, ANN202
            assert job_id == 7
            return "acme/demo"

        async def get_job_logs(self, repository, job_id, **kwargs):  # noqa: ANN001, ANN003, ANN202
            assert repository == "acme/demo", "the stored job names its repository"
            return view

        async def job_failure_history(self, repository, job_name, sample):  # noqa: ANN001, ANN202
            return None

    payload = asyncio.run(_log_tool(_Query())["get_job_log"](job_id=7))

    assert payload["source"] == "job"
    assert payload["content"] == "one"


def test_get_job_log_without_step_flags_still_windows_the_job_document() -> None:
    from rhosocial.github_ci_tracker.views import JobLogsView

    view = JobLogsView(job_id=7, job_name="build", log_content="one\ntwo\nthree\n")

    class _Query:
        async def get_job_logs(self, repository, job_id, **kwargs):  # noqa: ANN001, ANN003, ANN202
            assert kwargs == {"step": None, "failed_steps_only": False}
            return view

        async def job_failure_history(self, repository, job_name, sample):  # noqa: ANN001, ANN202
            return None

    payload = asyncio.run(
        _log_tool(_Query())["get_job_log"](repository="acme/demo", job_id=7, tail=1)
    )

    assert payload["source"] == "job"
    assert payload["content"] == "three"
    assert payload["total_lines"] == 3


def test_get_run_returns_a_roster_without_steps_or_a_failure_block() -> None:
    """Run level is run level: no step detail, no log excerpts, no duplication."""

    from rhosocial.github_ci_tracker.views import JobRecord, RunRecord

    run = RunRecord(
        run_id=9,
        status="completed",
        conclusion="failure",
        attempt=1,
        is_latest=True,
        attempts_total=1,
        jobs=[
            JobRecord(job_id=7, name="build", conclusion="failure", failed=True),
            JobRecord(job_id=8, name="lint", conclusion="success"),
        ],
    )

    class _Query:
        async def get_run_status(self, repository, run_id, attempt=None):  # noqa: ANN001, ANN202
            return run

        async def get_run_jobs(self, repository, run_id, with_steps=True):  # noqa: ANN001, ANN202
            assert with_steps is False, "the roster must not load steps"
            return run.jobs

    payload = asyncio.run(_log_tool(_Query())["get_run"](run_id=9, repository="acme/demo"))

    assert "failure" not in payload
    assert [job["job_id"] for job in payload["jobs"]] == [7, 8]
    assert payload["jobs"][0]["failed"] is True
    assert all("steps" not in job for job in payload["jobs"])
    assert "get_job(job_id=7)" in payload["hint"]


def test_get_job_names_the_failing_step_and_whether_its_log_is_here() -> None:
    from rhosocial.github_ci_tracker.views import JobRecord, StepRecord

    job = JobRecord(
        job_id=7,
        run_id=9,
        name="build",
        status="completed",
        conclusion="failure",
        failed=True,
        steps=[
            StepRecord(
                number=1, name="Set up job", conclusion="success", has_log=True, log_lines=12
            ),
            StepRecord(
                number=2,
                name="Run tests",
                conclusion="failure",
                failed=True,
                has_log=True,
                log_lines=40,
            ),
        ],
    )

    class _Query:
        async def repository_name_of_job(self, job_id):  # noqa: ANN001, ANN202
            return "acme/demo"

        async def get_job_detail(self, repository, job_id):  # noqa: ANN001, ANN202
            assert repository == "acme/demo", "resolved, not required from the caller"
            return job

    payload = asyncio.run(_log_tool(_Query())["get_job"](job_id=7))

    assert [step["number"] for step in payload["steps"]] == [1, 2]
    assert payload["steps"][1]["has_log"] is True
    assert "step=2" in payload["hint"]


def test_get_job_points_at_the_job_when_the_step_log_was_not_captured() -> None:
    from rhosocial.github_ci_tracker.views import JobRecord, StepRecord

    job = JobRecord(
        job_id=7,
        run_id=9,
        name="build",
        conclusion="failure",
        failed=True,
        steps=[
            StepRecord(number=2, name="Run tests", conclusion="failure", failed=True, has_log=False)
        ],
    )

    class _Query:
        async def repository_name_of_job(self, job_id):  # noqa: ANN001, ANN202
            return "acme/demo"

        async def get_job_detail(self, repository, job_id):  # noqa: ANN001, ANN202
            return job

    payload = asyncio.run(_log_tool(_Query())["get_job"](job_id=7))

    assert "whole job" in payload["hint"]


def _search_tool(view):
    class _Query:
        async def get_job_logs(self, repository, job_id, **kwargs):  # noqa: ANN001, ANN003, ANN202
            _Query.kwargs = kwargs
            return view

    tools = _log_tool(_Query())
    return tools["search_job_log"], _Query


def test_search_with_step_reads_that_step_and_reports_local_lines() -> None:
    from rhosocial.github_ci_tracker.views import JobLogsView, StepSection

    view = JobLogsView(
        job_id=7,
        job_name="build",
        source="step",
        sections=[StepSection(number=2, name="Run tests", content="up\nboom\nstill running\n")],
    )
    tool, query = _search_tool(view)

    payload = asyncio.run(tool(repository="acme/demo", job_id=7, query="boom", step=2))

    assert query.kwargs["step"] == 2
    assert payload["source"] == "step"
    assert payload["step"] == 2
    assert payload["matches"] == [2], "line numbers are local to the step"
    assert "step=2" in payload["hint"]


def test_search_without_step_stays_on_the_job_document() -> None:
    from rhosocial.github_ci_tracker.views import JobLogsView

    view = JobLogsView(job_id=7, job_name="build", log_content="up\nboom\n")
    tool, query = _search_tool(view)

    payload = asyncio.run(tool(repository="acme/demo", job_id=7, query="boom"))

    assert query.kwargs["step"] is None
    assert payload["source"] == "job"
    assert "step" not in payload
    assert payload["matches"] == [2]


def test_search_on_a_step_without_a_log_is_not_reported_as_no_match() -> None:
    """The document does not exist here, so the pattern was never searched."""
    from rhosocial.github_ci_tracker.views import JobLogsView

    view = JobLogsView(
        job_id=7,
        job_name="build",
        source="step",
        steps_without_logs=[2],
        reason="capture_closed",
    )
    tool, _ = _search_tool(view)

    payload = asyncio.run(tool(repository="acme/demo", job_id=7, query="boom", step=2))

    assert payload["matches"] == []
    assert payload["reason"] == "capture_closed"
    assert "nothing to search" in payload["hint"]
    assert "without step" in payload["hint"]


def test_step_mode_honours_an_explicit_line_window() -> None:
    """The regression a real run exposed: 236KB came back for a 3-line request."""
    from rhosocial.github_ci_tracker.views import JobLogsView, StepSection

    view = JobLogsView(
        job_id=7,
        job_name="build",
        source="step",
        sections=[
            StepSection(number=6, name="Run tests", content="one\ntwo\nthree\nfour\n", line_count=4)
        ],
    )

    class _Query:
        async def get_job_logs(self, repository, job_id, **kwargs):  # noqa: ANN001, ANN003, ANN202
            return view

    payload = asyncio.run(
        _log_tool(_Query())["get_job_log"](
            repository="acme/demo", job_id=7, step=6, from_line=2, to_line=3
        )
    )

    assert payload["content"] == "two\nthree"
    assert payload["first_line"] == 2
    assert payload["last_line"] == 3
    assert payload["total_lines"] == 4
    assert payload["truncated"] is True
    assert payload["next_from_line"] == 4


def test_step_mode_honours_grep() -> None:
    from rhosocial.github_ci_tracker.views import JobLogsView, StepSection

    view = JobLogsView(
        job_id=7,
        job_name="build",
        source="step",
        sections=[
            StepSection(number=6, name="Run tests", content="up\nboom\ndown\n", line_count=3)
        ],
    )

    class _Query:
        async def get_job_logs(self, repository, job_id, **kwargs):  # noqa: ANN001, ANN003, ANN202
            return view

    payload = asyncio.run(
        _log_tool(_Query())["get_job_log"](
            repository="acme/demo", job_id=7, step=6, grep="boom", context=0
        )
    )

    assert payload["content"] == "boom"
    assert payload["matches"] == [2]


def test_step_mode_without_arguments_returns_the_whole_step() -> None:
    from rhosocial.github_ci_tracker.views import JobLogsView, StepSection

    view = JobLogsView(
        job_id=7,
        job_name="build",
        source="step",
        sections=[StepSection(number=6, name="Run tests", content="one\ntwo\n", line_count=2)],
    )

    class _Query:
        async def get_job_logs(self, repository, job_id, **kwargs):  # noqa: ANN001, ANN003, ANN202
            return view

    payload = asyncio.run(
        _log_tool(_Query())["get_job_log"](repository="acme/demo", job_id=7, step=6)
    )

    assert payload["content"] == "one\ntwo\n"
    assert payload["truncated"] is False
    assert payload["returned_lines"] == 2


def test_get_job_refresh_logs_reports_what_the_refresh_did() -> None:
    from rhosocial.github_ci_tracker.views import (
        CaptureStatus,
        JobRecord,
        LogCaptureView,
        StepRecord,
    )

    job = JobRecord(
        job_id=7,
        run_id=9,
        name="build",
        conclusion="success",
        capture=CaptureStatus(expected_logs=2, captured_logs=2, state="complete"),
        steps=[StepRecord(number=1, name="Set up job", conclusion="success", has_log=True)],
    )

    class _Query:
        async def repository_name_of_job(self, job_id):  # noqa: ANN001, ANN202
            return "acme/demo"

        async def refresh_step_logs(self, repository, job_id):  # noqa: ANN001, ANN202
            return LogCaptureView(run_id=9, attempts=3, steps_stored=2, missing_steps=[])

        async def get_job_detail(self, repository, job_id):  # noqa: ANN001, ANN202
            return job

    payload = asyncio.run(_log_tool(_Query())["get_job"](job_id=7, refresh_logs=True))

    assert payload["refresh"]["attempts"] == 3
    assert payload["refresh"]["stored"] == 2
    assert payload["capture"]["state"] == "complete"


def test_get_job_without_refresh_does_not_capture() -> None:
    from rhosocial.github_ci_tracker.views import CaptureStatus, JobRecord, StepRecord

    job = JobRecord(
        job_id=7,
        run_id=9,
        name="build",
        conclusion="success",
        capture=CaptureStatus(expected_logs=2, captured_logs=1, state="partial"),
        steps=[StepRecord(number=1, name="Set up job", conclusion="success", has_log=True)],
    )

    class _Query:
        async def repository_name_of_job(self, job_id):  # noqa: ANN001, ANN202
            return "acme/demo"

        async def refresh_step_logs(self, repository, job_id):  # noqa: ANN001, ANN202
            raise AssertionError("refresh_logs was not asked for")

        async def get_job_detail(self, repository, job_id):  # noqa: ANN001, ANN202
            return job

    payload = asyncio.run(_log_tool(_Query())["get_job"](job_id=7))

    assert "refresh" not in payload
    assert "refresh_logs=true" in payload["hint"], "partial tells the caller how to wait"


def test_the_capture_window_is_reported_in_utc() -> None:
    from datetime import datetime, timedelta, timezone

    from rhosocial.github_ci_tracker.views import CaptureStatus, JobRecord, StepRecord

    shanghai = timezone(timedelta(hours=8))
    job = JobRecord(
        job_id=7,
        run_id=9,
        name="build",
        conclusion="success",
        capture=CaptureStatus(
            expected_logs=1,
            captured_logs=1,
            state="complete",
            window_closes_at=datetime(2026, 10, 31, 6, 5, 6, tzinfo=shanghai),
        ),
        steps=[StepRecord(number=1, name="Set up job", conclusion="success", has_log=True)],
    )

    class _Query:
        async def repository_name_of_job(self, job_id):  # noqa: ANN001, ANN202
            return "acme/demo"

        async def get_job_detail(self, repository, job_id):  # noqa: ANN001, ANN202
            return job

    payload = asyncio.run(_log_tool(_Query())["get_job"](job_id=7))

    assert payload["capture"]["window_closes_at"].endswith("+00:00")
