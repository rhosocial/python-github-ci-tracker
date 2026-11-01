# tests/conftest.py
"""Shared test fixtures: a clean PostgreSQL schema per test, plus a fake client.

Every business-core test runs against a real PostgreSQL server: the models are
bound to one connection, the schema is rebuilt from scratch, and GitHub is
driven through :class:`FakeGithub` -- an in-memory stand-in for the async
client, so no HTTP call to GitHub is ever attempted.

The server comes from the environment (``CITRACKER_DB_HOST`` and friends, or a
scenarios file); :mod:`tests.pg` owns that contract, and a test without a
configured server skips rather than invents one.

Every test that needs a database names the :func:`bound` fixture, and pytest
owns its lifetime: setup before the yield, teardown after it. No coroutine
is wrapped in ``asyncio.run`` -- ``asyncio_mode = "auto"`` in pyproject.toml lets
a test be a plain ``async def``, which is what makes a fixture possible at all.
The sync twins stay unbound: the behavior under test is the async pair.
"""

from __future__ import annotations

import asyncio
import sys
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import pytest_asyncio
from rhosocial.activerecord.backend.impl.postgres.backend import AsyncPostgresBackend
from rhosocial.activerecord.backend.impl.postgres.config import PostgresConnectionConfig

from rhosocial.github_ci_tracker.core.exceptions import GithubAPIError
from rhosocial.github_ci_tracker.credentials import AsyncPatCredential
from rhosocial.github_ci_tracker.database import SET_TIME_ZONE
from rhosocial.github_ci_tracker.models import (
    AsyncJob,
    AsyncJobLog,
    AsyncRepository,
    AsyncRepositoryBinding,
    AsyncRunAttempt,
    AsyncStep,
    AsyncStepLog,
    AsyncWebhookDelivery,
    AsyncWorkflow,
    AsyncWorkflowRun,
)
from rhosocial.github_ci_tracker.schema import TABLES, create_all

#: Every async model bound by the :func:`bound` fixture.
#: The session-scoped backend :func:`bound_schema` reuses. None until the
#: first test asks for a schema; one connection for the whole run.
_SESSION_BACKEND: AsyncPostgresBackend | None = None
ASYNC_MODELS: tuple[type, ...] = (
    AsyncPatCredential,
    AsyncRepository,
    AsyncRepositoryBinding,
    AsyncWorkflow,
    AsyncWorkflowRun,
    AsyncRunAttempt,
    AsyncJob,
    AsyncStep,
    AsyncStepLog,
    AsyncJobLog,
    AsyncWebhookDelivery,
)


class FakeGithub:
    """In-memory fake of the async GitHub client.

    Mirrors GitHub's payload shapes with knobs for the failure cases the tests
    need: invalid tokens, inaccessible repositories and anonymous reads of
    private repositories.
    """

    def __init__(
        self,
        *,
        repo_info: dict[str, Any] | None = None,
        workflows: list[dict[str, Any]] | None = None,
        runs: list[dict[str, Any]] | None = None,
        jobs: list[dict[str, Any]] | None = None,
        logs: dict[str, str] | None = None,
        token_valid: bool = True,
        inaccessible_repos: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        self.token_valid = token_valid
        self.verify_calls = 0
        self.repo_info_calls = 0
        self.inaccessible_repos = frozenset(inaccessible_repos)
        self.repo_info = (
            repo_info
            if repo_info is not None
            else {
                "id": 99,
                "owner": {"login": "acme"},
                "name": "demo",
                "full_name": "acme/demo",
                "is_private": True,
                "default_branch": "main",
            }
        )
        self.workflows = (
            workflows
            if workflows is not None
            else [{"id": 7, "name": "CI", "path": ".github/workflows/ci.yml", "state": "active"}]
        )
        self.runs = (
            runs
            if runs is not None
            else [
                {
                    "id": 500,
                    "run_number": 1,
                    "event": "workflow_dispatch",
                    "status": "completed",
                    "conclusion": "success",
                    "head_branch": "main",
                    "head_sha": "abc",
                    "head_commit": {"message": "fix stuff"},
                    "actor": {"login": "alice"},
                    "run_started_at": "2026-10-10T10:00:00Z",
                    "completed_at": "2026-10-10T10:05:00Z",
                    "workflow_id": 7,
                    "path": ".github/workflows/ci.yml",
                    "name": "CI",
                }
            ]
        )
        self.jobs = (
            jobs
            if jobs is not None
            else [
                {
                    "id": 900,
                    "run_id": 500,
                    "name": "build",
                    "status": "completed",
                    "conclusion": "success",
                    "runner_name": "ubuntu-latest",
                    "started_at": "2026-10-10T10:00:00Z",
                    "completed_at": "2026-10-10T10:05:00Z",
                    "steps": [
                        {
                            "number": 1,
                            "name": "checkout",
                            "status": "completed",
                            "conclusion": "success",
                            "started_at": "2026-10-10T10:00:00Z",
                            "completed_at": "2026-10-10T10:01:00Z",
                        }
                    ],
                }
            ]
        )
        self.logs = logs if logs is not None else {"900": "line1\nline2\nline3"}
        #: Pull requests keyed by number; the runs endpoint is filtered by head
        #: commit rather than by PR number, so the fake mirrors that split.
        self.pull_requests: dict[int, dict[str, Any]] = {}
        self.created_hooks: list[dict[str, Any]] = []
        self.deleted_hook_ids: list[int] = []
        #: Dispatch attempts. Counting them is how a test proves that a refused
        #: trigger never reached the client.
        self.trigger_calls = 0
        #: Run ids passed to rerun / cancel, so a test can assert the request
        #: reached the client rather than only that the service returned.
        self.rerun_calls: list[int] = []
        self.cancel_calls: list[int] = []
        #: Run ids read with ``get_run``. Several deliveries of one run must
        #: not each pay for that read.
        self.run_calls: list[int] = []
        #: Job ids whose log was fetched, so a test can prove the stored row is
        #: what answers the second read.
        self.log_calls: list[int] = []
        #: Run ids whose archive was fetched. ``None`` makes the call 404, which
        #: is what an expired or not-yet-ready archive looks like.
        self.run_log_calls: list[int] = []
        #: Job ids read through the single-job endpoint (the healing paths).
        self.get_job_calls: list[int] = []
        self.run_log_archive: bytes | None = b""

        #: A helper the archive tests share: build a zip from ``{name: text}``.
        self.make_archive = make_archive
        #: Run ids whose job list was fetched, for the "terminal rows are served
        #: from the cache" rule.
        self.jobs_calls: list[int] = []
        #: Attempt payloads keyed by ``(run id, attempt)``. A read of an attempt
        #: that is not here is a 404, exactly like the real endpoint.
        self.attempt_payloads: dict[tuple[int, int], dict[str, Any]] = {}
        #: ``(run id, attempt)`` pairs read through the attempts endpoint.
        self.attempt_calls: list[tuple[int, int]] = []
        #: The commit each ref points at. ``None`` means "the newest run's sha",
        #: which is what a test that does not care about the head expects.
        self.ref_shas: dict[str, str] = {}
        #: Refs whose head commit was asked for.
        self.ref_calls: list[str] = []

    async def verify_token(self, token: str, probe_repo: str | None = None) -> dict[str, Any]:
        """Probe ``/user`` and ``/user/repos``; validity follows ``token_valid``.

        ``probe_repo`` mirrors the real client's Actions-write probe. This fake
        reports a read-only credential (``can_write_actions`` false), which is
        the broader public-PAT case; tests that need the write path stub the
        client instead.
        """
        del probe_repo
        self.verify_calls += 1
        return {
            "is_valid": self.token_valid,
            "username": "alice" if self.token_valid else "",
            "can_read_actions": self.token_valid,
            "can_write_actions": False,
            "can_manage_webhooks": self.token_valid,
            "accessible_repos": 3 if self.token_valid else 0,
        }

    async def get_repo_info(self, token: str | None, repo_full_name: str) -> dict[str, Any]:
        """Return repository metadata, 404-ing for inaccessible/private reads."""
        self.repo_info_calls += 1
        if repo_full_name in self.inaccessible_repos:
            raise GithubAPIError("not found", status_code=404)
        if token is None and self.repo_info.get("is_private"):
            # GitHub answers 404 for private repositories when unauthenticated.
            raise GithubAPIError("not found", status_code=404)
        return dict(self.repo_info)

    async def list_accessible_repos(self, token: str) -> list[dict[str, Any]]:
        """Return the single fake repository."""
        return [dict(self.repo_info)]

    async def get_repo_workflows(
        self, token: str | None, repo_full_name: str
    ) -> list[dict[str, Any]]:
        """Return the fake workflow list."""
        return [dict(workflow) for workflow in self.workflows]

    async def trigger_workflow(
        self,
        token: str,
        repo_full_name: str,
        workflow_id: str,
        ref: str,
        inputs: dict[str, str] | None = None,
        return_run_details: bool = True,
    ) -> dict[str, Any]:
        """Accept every dispatch; run resolution is up to the caller."""
        del inputs, return_run_details
        self.trigger_calls += 1
        return {"run_id": 0, "status": "queued"}

    async def rerun_workflow(self, token: str, repo_full_name: str, run_id: int) -> None:
        """Record the re-run and reproduce GitHub's semantics.

        GitHub does **not** create a run for a re-run: the id stays the same and
        ``run_attempt`` advances, with the run put back into a non-terminal
        state. A fake that only recorded the call is what let the original bug
        live -- the service looked for a *new* run id, and nothing complained.
        """
        self.rerun_calls.append(run_id)
        for index, run in enumerate(self.runs):
            if run.get("id") == run_id:
                updated = dict(run)
                updated["run_attempt"] = int(updated.get("run_attempt") or 1) + 1
                updated["status"] = "queued"
                updated["conclusion"] = None
                updated["completed_at"] = None
                self.runs[index] = updated
                return
        raise GithubAPIError("missing", status_code=404)

    async def cancel_workflow_run(self, token: str, repo_full_name: str, run_id: int) -> None:
        """Record the cancel request."""
        self.cancel_calls.append(run_id)

    async def get_run(self, token: str | None, repo_full_name: str, run_id: int) -> dict[str, Any]:
        """Return the fake run payload, 404-ing for unknown ids."""
        self.run_calls.append(run_id)
        for run in self.runs:
            if run["id"] == run_id:
                return dict(run)
        raise GithubAPIError("missing", status_code=404)

    async def list_runs(
        self,
        token: str | None,
        repo_full_name: str,
        workflow_id: str | None = None,
        status: str | None = None,
        branch: str | None = None,
        head_sha: str | None = None,
        event: str | None = None,
        per_page: int = 30,
        page: int = 1,
        all_pages: bool = False,
    ) -> list[dict[str, Any]]:
        """Return the fake run list, honoring the filters the real client sends.

        The real client filters server-side; the fake does the same locally so a
        pull-request lookup cannot accidentally match an unrelated run. ``branch``
        matters as much as the rest: a fake that ignored it let ``get_latest_run``
        pass while silently answering about the default branch.
        """
        runs = list(self.runs)
        if head_sha is not None:
            runs = [run for run in runs if run.get("head_sha") == head_sha]
        if event is not None:
            runs = [run for run in runs if run.get("event") == event]
        if branch is not None:
            runs = [run for run in runs if run.get("head_branch") == branch]
        if workflow_id is not None:
            runs = [run for run in runs if str(run.get("workflow_id")) == str(workflow_id)]
        return [dict(run) for run in runs]

    async def get_ref_sha(self, token: str | None, repo_full_name: str, ref: str) -> str:
        """Return the commit ``ref`` points at, defaulting to the newest run's."""
        self.ref_calls.append(ref)
        if ref in self.ref_shas:
            return self.ref_shas[ref]
        return str(self.runs[0].get("head_sha") or "") if self.runs else ""

    async def get_pull_request(
        self, token: str | None, repo_full_name: str, number: int
    ) -> dict[str, Any]:
        """Return a stored pull request payload, 404-ing for unknown numbers."""
        pull = self.pull_requests.get(number)
        if pull is None:
            raise GithubAPIError("missing pull request", status_code=404)
        return dict(pull)

    async def get_run_attempt(
        self, token: str | None, repo_full_name: str, run_id: int, attempt: int
    ) -> dict[str, Any]:
        """Return the fake payload for one attempt, 404-ing when there is none."""
        self.attempt_calls.append((run_id, attempt))
        payload = self.attempt_payloads.get((run_id, attempt))
        if payload is None:
            raise GithubAPIError("not found", status_code=404)
        return dict(payload)

    async def get_run_jobs(
        self, token: str | None, repo_full_name: str, run_id: int
    ) -> list[dict[str, Any]]:
        """Return the fake jobs of a run (filtered by ``run_id``)."""
        self.jobs_calls.append(run_id)
        return [dict(job) for job in self.jobs if job.get("run_id") == run_id]

    async def get_job(self, token: str | None, repo_full_name: str, job_id: int) -> dict[str, Any]:
        """Return the stored fake job payload."""
        self.get_job_calls.append(job_id)
        for job in self.jobs:
            if job.get("id") == job_id:
                return dict(job)
        raise GithubAPIError("missing", status_code=404)

    async def get_run_logs(self, token: str | None, repo_full_name: str, run_id: int) -> bytes:
        """Return the fake run archive; ``None`` means GitHub has none."""
        self.run_log_calls.append(run_id)
        if self.run_log_archive is None:
            raise GithubAPIError("no log archive", status_code=404)
        return self.run_log_archive

    async def get_job_logs(self, token: str | None, repo_full_name: str, job_id: int) -> str:
        """Return the fake log document for a job id."""
        self.log_calls.append(job_id)
        return self.logs.get(str(job_id), "")

    async def create_webhook(
        self, token: str, repo_full_name: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        """Record a created hook; ids count up from 1."""
        hook = {"id": len(self.created_hooks) + 1, "config": dict(config)}
        self.created_hooks.append(hook)
        return dict(hook)

    async def list_webhooks(self, token: str, repo_full_name: str) -> list[dict[str, Any]]:
        """Return the hooks created through this fake."""
        return [dict(hook) for hook in self.created_hooks]

    async def delete_webhook(self, token: str, repo_full_name: str, hook_id: int) -> None:
        """Record the deleted hook id."""
        self.deleted_hook_ids.append(hook_id)


@pytest.fixture
async def bound(bound_models: AsyncPostgresBackend) -> AsyncIterator[None]:
    """Run one test against a freshly built schema, every async model bound.

    One backend for all models, because that is what a request scope gives them
    in production: a transaction opened through one model covers the others, and
    a per-class backend would make the write-scope tests meaningless.

    Setup is everything before ``yield``, teardown everything after -- the schema
    is dropped and rebuilt by :func:`bound_models` for the next test. Pytest owns
    that lifetime, which is the point: a test that forgot the fixture would run
    without a database at all.
    """
    yield


def make_token_service(client: FakeGithub | None = None):
    """The credential service over a fake probe."""
    from rhosocial.github_ci_tracker.controllers.token import TokenController
    from rhosocial.github_ci_tracker.services.github import GithubCredentials

    return TokenController(probe=GithubCredentials(client or FakeGithub()))


def make_webhook_service(
    *,
    client: FakeGithub | None = None,
    tracker=None,
    log_scope=None,
    credential=None,
    logs=None,
):
    """The push-channel service over a fake transport.

    One helper for the whole suite: the service takes protocols and
    controllers, so every webhook test would otherwise repeat the same six
    lines of wiring and drift from the container's.
    """
    from rhosocial.github_ci_tracker.controllers import RepositoryData, RunData
    from rhosocial.github_ci_tracker.controllers.run import RunController
    from rhosocial.github_ci_tracker.controllers.webhook import DeliveryLedger, WebhookController
    from rhosocial.github_ci_tracker.services.github import GithubRuns, GithubWebhooks

    fake = client or FakeGithub()
    readers = GithubRuns(fake, credential=credential)
    runs_data = RunData()
    return WebhookController(
        webhooks=GithubWebhooks(fake, credential=credential),
        readers=readers,
        runs=RunController(runs=readers, runs_data=runs_data),
        repositories=RepositoryData(),
        runs_data=runs_data,
        deliveries=DeliveryLedger(),
        tracker=tracker,
        log_scope=log_scope,
        logs=logs,
    )


@pytest.fixture
def fake_client() -> FakeGithub:
    """A fresh in-memory GitHub client for one test."""
    return FakeGithub()


def make_repository_service(client: FakeGithub | None = None, **kwargs: Any):
    """The subscription service over a fake transport."""
    from rhosocial.github_ci_tracker.controllers import RepositoryData, RunData
    from rhosocial.github_ci_tracker.controllers.query import QueryController
    from rhosocial.github_ci_tracker.controllers.repository import RepositoryController
    from rhosocial.github_ci_tracker.controllers.run import RunController
    from rhosocial.github_ci_tracker.services.github import (
        GithubRepositories,
        GithubRuns,
        GithubWebhooks,
    )

    fake = client or FakeGithub()
    readers = GithubRuns(fake)
    runs_data = RunData()
    runs = RunController(runs=readers, runs_data=runs_data)
    return RepositoryController(
        repositories=GithubRepositories(fake),
        webhooks=GithubWebhooks(fake),
        readers=readers,
        runs=runs,
        data=RepositoryData(),
        runs_data=runs_data,
        query=QueryController(
            readers=readers,
            repositories=GithubRepositories(fake),
            runs=runs,
            runs_data=runs_data,
        ),
        **kwargs,
    )


async def subscribe(
    fake: FakeGithub,
    full_name: str = "acme/public",
    token_id: int | None = None,
    **kwargs: Any,
) -> Any:
    """Subscribe through the service, the way every adapter does.

    The service, not the model, is the entry point for GitHub access; tests that
    only need a subscription use this instead of reaching for the model.
    """
    return await make_repository_service(fake).subscribe_repository(
        full_name, token_id=token_id, **kwargs
    )


def make_archive(entries: dict[str, str]) -> bytes:
    """A run-log archive with exactly these entries, for tests.

    GitHub's archive is a zip of ``<job>/<step>.txt`` and ``<n>_<job>.txt``
    files; this builds one without pretending to be GitHub. Kept as a free
    function because it is test scaffolding, not behaviour.
    """
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, text in entries.items():
            bundle.writestr(name, text)
    return buffer.getvalue()


class FakeGithubReads:
    """Implements the read half of ``IGithubClient`` and records every call.

    ``FakeGithub`` below fakes the whole protocol for the service tests; this one
    exists for the GitHub read groups, which need the opposite property: a record
    of *how* they called the transport. The interesting assertion there is not
    only "what came back" but "which token was passed", which is the one thing
    the old controllers got wrong by omitting it.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error: Exception | None = None

        self.run_payload: dict[str, Any] = {}
        self.attempt_payload: dict[str, Any] = {}
        self.run_payloads: list[dict[str, Any]] = []
        self.job_payload: dict[str, Any] = {}
        self.job_payloads: list[dict[str, Any]] = []
        self.log_text = ""
        #: The run archive, as bytes. Empty means "GitHub served nothing"; a test
        #: that needs a real archive builds one with :func:`make_archive`.
        self.archive_bytes: bytes = b""
        self.repo_payload: dict[str, Any] = {}
        self.ref_sha = """"""
        self.workflow_payloads: list[dict[str, Any]] = []
        self.accessible_payloads: list[dict[str, Any]] = []
        self.dispatch_payload: dict[str, Any] = {"run_id": 0, "status": "queued"}
        self.webhook_payloads: list[dict[str, Any]] = []
        self.verify_payload: dict[str, Any] = {}
        self.can_write = False

    def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((method, kwargs))
        if self.error is not None:
            raise self.error

    async def get_run(self, token, repo_full_name, run_id) -> dict[str, Any]:
        self._record("get_run", {"token": token, "repo": repo_full_name, "run_id": run_id})
        return self.run_payload

    async def get_run_attempt(self, token, repo_full_name, run_id, attempt) -> dict[str, Any]:
        self._record(
            "get_run_attempt",
            {
                "token": token,
                "repo": repo_full_name,
                "run_id": run_id,
                "attempt": attempt,
            },
        )
        return self.attempt_payload

    async def list_runs(
        self,
        token,
        repo_full_name,
        workflow_id=None,
        status=None,
        branch=None,
        head_sha=None,
        per_page=30,
        page=1,
    ) -> list[dict[str, Any]]:
        self._record(
            "list_runs",
            {
                "token": token,
                "repo": repo_full_name,
                "workflow_id": workflow_id,
                "status": status,
                "branch": branch,
                "head_sha": head_sha,
                "per_page": per_page,
                "page": page,
            },
        )
        return self.run_payloads

    async def get_run_jobs(self, token, repo_full_name, run_id) -> list[dict[str, Any]]:
        self._record("get_run_jobs", {"token": token, "repo": repo_full_name, "run_id": run_id})
        return self.job_payloads

    async def get_job(self, token, repo_full_name, job_id) -> dict[str, Any]:
        self._record("get_job", {"token": token, "repo": repo_full_name, "job_id": job_id})
        return self.job_payload

    async def get_run_logs(self, token, repo_full_name, run_id) -> bytes:
        self._record("get_run_logs", {"token": token, "repo": repo_full_name, "run_id": run_id})
        return self.archive_bytes

    async def get_job_logs(self, token, repo_full_name, job_id) -> str:
        self._record("get_job_logs", {"token": token, "repo": repo_full_name, "job_id": job_id})
        return self.log_text

    async def get_ref_sha(self, token, repo_full_name, ref) -> str:
        self._record("get_ref_sha", {"token": token, "repo": repo_full_name, "ref": ref})
        return self.ref_sha

    async def get_repo_info(self, token, repo_full_name) -> dict[str, Any]:
        self._record("get_repo_info", {"token": token, "repo": repo_full_name})
        return self.repo_payload

    async def get_repo_workflows(self, token, repo_full_name) -> list[dict[str, Any]]:
        self._record("get_repo_workflows", {"token": token, "repo": repo_full_name})
        return self.workflow_payloads

    async def list_accessible_repos(self, token) -> list[dict[str, Any]]:
        self._record("list_accessible_repos", {"token": token})
        return self.accessible_payloads

    async def trigger_workflow(
        self,
        token,
        repo_full_name,
        workflow_id,
        ref,
        inputs=None,
        return_run_details=True,
    ) -> dict[str, Any]:
        self._record(
            "trigger_workflow",
            {
                "token": token,
                "repo": repo_full_name,
                "workflow_id": workflow_id,
                "ref": ref,
                "inputs": inputs,
            },
        )
        return self.dispatch_payload

    async def rerun_workflow(self, token, repo_full_name, run_id) -> None:
        self._record("rerun_workflow", {"token": token, "repo": repo_full_name, "run_id": run_id})

    async def cancel_workflow_run(self, token, repo_full_name, run_id) -> None:
        self._record(
            "cancel_workflow_run", {"token": token, "repo": repo_full_name, "run_id": run_id}
        )

    async def create_webhook(self, token, repo_full_name, config) -> dict[str, Any]:
        self._record("create_webhook", {"token": token, "repo": repo_full_name, "config": config})
        return {"id": 31_337, **config}

    async def list_webhooks(self, token, repo_full_name) -> list[dict[str, Any]]:
        self._record("list_webhooks", {"token": token, "repo": repo_full_name})
        return self.webhook_payloads

    async def delete_webhook(self, token, repo_full_name, hook_id) -> None:
        self._record("delete_webhook", {"token": token, "repo": repo_full_name, "hook_id": hook_id})

    async def ping_webhook(self, token, repo_full_name, hook_id) -> None:
        self._record("ping_webhook", {"token": token, "repo": repo_full_name, "hook_id": hook_id})

    async def verify_token(self, token, probe_repo=None) -> dict[str, Any]:
        self._record("verify_token", {"token": token, "probe_repo": probe_repo})
        return self.verify_payload

    async def probe_actions_write(self, token, repo_full_name) -> bool:
        self._record("probe_actions_write", {"token": token, "repo": repo_full_name})
        return self.can_write


class StubCredential:
    """The two attributes a GitHub group reads off a resolved credential."""

    def __init__(self, token: str = "ghp_test_token", identity: str = "alice") -> None:
        self.token = token
        self.identity = identity


@pytest.fixture
def read_transport() -> FakeGithubReads:
    """A read-only transport with nothing stubbed; each test fills in what it reads."""
    return FakeGithubReads()


@pytest.fixture
def credential() -> StubCredential:
    """A resolved credential, shaped like the real one for what the groups read."""
    return StubCredential()


def _ensure_compatible_event_loop() -> None:
    """Make the default loop one psycopg will accept on Windows.

    psycopg refuses the ProactorEventLoop, which is what ``asyncio`` installs by
    default there, so every async fixture would fail on a Windows machine and pass
    on Linux -- a difference that has nothing to do with the code under test. The
    backend test suites in python-activerecord carry the same shim; CI runs on
    Linux, where the default is already correct.
    """
    if sys.platform == "win32":
        # Deprecated in 3.14 and slated for removal in 3.16 -- the same call the
        # backend test suites make, and the only one that reaches the loop
        # pytest-asyncio creates. Silenced here rather than printed on every
        # import on every Windows run; on Linux the branch never executes.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            policy = asyncio.get_event_loop_policy()
            if not isinstance(policy, asyncio.WindowsSelectorEventLoopPolicy):
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


_ensure_compatible_event_loop()

# ---------------------------------------------------------------------------
# PostgreSQL: one server, one database, prepared before the session runs
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_scenario() -> dict[str, Any]:
    """The postgres_18 connection parameters, or a skip that says why.

    The environment contract -- which variables, which file, in what order --
    lives in :mod:`tests.pg`; this is only the fixture form of it.
    """
    from pg import resolve_scenario

    scenario = resolve_scenario()
    if scenario is None:
        pytest.skip(
            "no PostgreSQL configured: set CITRACKER_DB_HOST (or PGHOST), or point "
            "CITRACKER_TEST_PG_SCENARIO_FILE at a scenarios file"
        )
    return scenario


@pytest_asyncio.fixture(scope="session")
async def pg_database(pg_scenario: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """Ensure ``citracker_test`` exists on that server, then hand back its address.

    The database is named once, for every run, and created rather than assumed: a
    suite that needs a database the operator already made by hand is a suite that
    fails on the first machine. ``ensure_database`` is the application's own
    bootstrap -- connecting through the maintenance database, because PostgreSQL
    refuses ``CREATE DATABASE`` from inside the database being created.

    The database is not dropped on the way out. It is the fixture, not the
    sandbox: each test gets a clean schema inside it (see :func:`bound_models`),
    so an aborted run leaves nothing behind but an empty database.
    """
    from pg import TEST_DATABASE

    from rhosocial.github_ci_tracker.database import PostgresServer

    config = PostgresConnectionConfig(**{**pg_scenario, "database": TEST_DATABASE})
    await PostgresServer(config).ensure()
    yield {**pg_scenario, "database": TEST_DATABASE}


@asynccontextmanager
async def bound_schema(
    models: tuple[type, ...] = ASYNC_MODELS,
    scenario: dict[str, Any] | None = None,
) -> AsyncIterator[AsyncPostgresBackend]:
    """Bind ``models`` to one clean schema for one test.

    One backend for all models, not one per class: foreign keys and the two-hop
    reads assume they share a connection, and a per-class backend is exactly the
    mistake ``database.py`` documents.

    The schema is built once per session and truncated between tests. Rebuilding
    thirteen tables per test costs more than the tests themselves -- four minutes
    against a remote server -- and truncation gives the same guarantee: no rows
    survive, and ``RESTART IDENTITY`` resets the sequences so ids stay comparable
    from test to test.

    This is also the context-manager form, for the callers that cannot take a
    fixture (a TestClient lifespan needs somewhere to bind from).
    """
    global _SESSION_BACKEND
    if _SESSION_BACKEND is None:
        if scenario is None:
            from pg import resolve_scenario

            resolved = resolve_scenario()
            if resolved is None:
                pytest.skip(
                    "no PostgreSQL configured: set CITRACKER_DB_HOST (or PGHOST), or point "
                    "CITRACKER_TEST_PG_SCENARIO_FILE at a scenarios file"
                )
            scenario = dict(resolved or {})
        from pg import TEST_DATABASE

        scenario = {**scenario, "database": TEST_DATABASE}
        backend = AsyncPostgresBackend(connection_config=PostgresConnectionConfig(**scenario))
        await backend.connect()
        await backend.execute(SET_TIME_ZONE)
        await backend.introspect_and_adapt()

        # Prove the isolation instead of trusting it: a schema that survived the
        # drop would let one test's rows answer another test's query.
        await backend.execute("DROP SCHEMA public CASCADE")
        await backend.execute("CREATE SCHEMA public")
        remaining = await backend.execute(
            "SELECT count(*) AS n FROM information_schema.tables "
            "WHERE table_schema = current_schema()"
        )
        if (remaining.data or [{}])[0].get("n"):
            raise RuntimeError("the schema was not empty after being rebuilt")
        await create_all(backend)
        _SESSION_BACKEND = backend
    else:
        backend = _SESSION_BACKEND
        await backend.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")

    for model in models:
        model.__backend__ = backend
    try:
        yield backend
    finally:
        for model in models:
            model.__backend__ = None


@pytest_asyncio.fixture
async def bound_models(pg_database: dict[str, Any]) -> AsyncIterator[AsyncPostgresBackend]:
    """One clean schema in ``citracker_test``, with every async twin bound to it."""
    async with bound_schema(ASYNC_MODELS, pg_database) as backend:
        yield backend
