# src/rhosocial/github_ci_tracker/services/github/protocols.py
"""Capability protocols: what a caller may assume about a GitHub access object.

Grouped by what a caller needs, not by which engine implements it -- the same
split the SQL backends use for their expression surface. A service names the
protocol it depends on and receives anything that satisfies it: the HTTP
implementation in production, a recording fake in a test, a partial object when a
test only exercises one verb.

Nothing here is an interface class to inherit from. These are structural types:
the implementation simply has the methods, and ``isinstance`` works because each
protocol is runtime-checkable. That is what keeps a fake honest without making it
import a base class and inherit behaviour it does not want.

``Mapping`` rather than a concrete type on the way back, on purpose: the models
already know how to read a payload (``adopt_from_remote``, ``update_from_remote``,
``upsert_from_remote``, ``sync_jobs_from_remote``). A hand-written object here
would be a second place that knows the field names, and the two would drift.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # the parsed archive; annotations only, no import cycle
    from .logs import RunLogArchive


@runtime_checkable
class RunReader(Protocol):
    """Read one run, a page of them, its jobs, and one job's log."""

    async def get_run(self, repo_full_name: str, run_id: int) -> Mapping[str, Any]:
        """GET ``/repos/{repo}/actions/runs/{run_id}`` (the latest attempt)."""
        ...

    async def get_run_attempt(
        self, repo_full_name: str, run_id: int, attempt: int
    ) -> Mapping[str, Any]:
        """GET ``/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}``.

        A run id alone names the *latest* attempt; a past one has to be asked
        for by number.
        """
        ...

    async def list_runs(
        self,
        repo_full_name: str,
        *,
        workflow_id: int | str | None = None,
        status: str | None = None,
        branch: str | None = None,
        head_sha: str | None = None,
        per_page: int = 30,
        page: int = 1,
    ) -> list[Mapping[str, Any]]:
        """GET ``/repos/{repo}/actions/runs``.

        ``head_sha`` is what makes a pull request's runs addressable: the
        endpoint has no PR filter.
        """
        ...

    async def get_ref_sha(self, repo_full_name: str, ref: str) -> str:
        """GET ``/repos/{repo}/commits/{ref}``, reduced to the sha.

        A run names the commit it executes; only the ref says which commit that
        should be now, which is what makes "already dispatched for this head"
        answerable without guessing.
        """
        ...

    async def get_run_jobs(self, repo_full_name: str, run_id: int) -> list[Mapping[str, Any]]:
        """GET ``/repos/{repo}/actions/runs/{run_id}/jobs`` (latest attempt)."""
        ...

    async def get_run_logs(self, repo_full_name: str, run_id: int) -> RunLogArchive:
        """GET ``/repos/{repo}/actions/runs/{run_id}/logs``, parsed.

        The archive carries per-step files only for about an hour after the run
        finishes; :attr:`RunLogArchive.form` reports which layout arrived, and
        :meth:`steps_for` answers per job.
        """
        ...

    async def get_job(self, repo_full_name: str, job_id: int) -> Mapping[str, Any]:
        """GET ``/repos/{repo}/actions/jobs/{job_id}``, steps included.

        The list endpoint is per run; this one exists for a single job whose
        payload arrived incomplete or whose row is stale.
        """
        ...

    async def get_job_logs(self, repo_full_name: str, job_id: int) -> str:
        """GET ``/repos/{repo}/actions/jobs/{job_id}/logs``."""
        ...


@runtime_checkable
class RunControl(Protocol):
    """Dispatch, re-run and cancel. Every one of these spends CI minutes."""

    async def dispatch(
        self,
        repo_full_name: str,
        workflow_id: str,
        ref: str,
        inputs: dict[str, str] | None = None,
    ) -> Mapping[str, Any]:
        """POST ``/repos/{repo}/actions/workflows/{id}/dispatches``.

        The answer may carry the created run's id, or nothing at all -- a server
        that ignores ``return_run_details`` answers ``204``.
        """
        ...

    async def rerun(self, repo_full_name: str, run_id: int) -> None:
        """POST ``/repos/{repo}/actions/runs/{run_id}/rerun``."""
        ...

    async def cancel(self, repo_full_name: str, run_id: int) -> None:
        """POST ``/repos/{repo}/actions/runs/{run_id}/cancel``."""
        ...


@runtime_checkable
class RepositoryReader(Protocol):
    """Repository metadata, its workflows, and what a credential can see."""

    async def get_repo_info(
        self, repo_full_name: str, credential: Any | None = None
    ) -> Mapping[str, Any]:
        """GET ``/repos/{owner}/{repo}``.

        ``credential`` overrides the ambient one: "can *this* credential see the
        repository" is the question a subscription asks.
        """
        ...

    async def get_repo_workflows(self, repo_full_name: str) -> list[Mapping[str, Any]]:
        """GET ``/repos/{owner}/{repo}/actions/workflows``."""
        ...

    async def get_pull_request(self, repo_full_name: str, number: int) -> Mapping[str, Any]:
        """GET ``/repos/{owner}/{repo}/pulls/{number}``."""
        ...

    async def list_accessible_repos(self, credential: Any) -> list[Mapping[str, Any]]:
        """GET ``/user/repos`` as ``credential``.

        The credential is the subject of the call, not ambient context: the
        answer is "what can *this* see", so passing it is the point.
        """
        ...


@runtime_checkable
class WebhookManager(Protocol):
    """Create, list, delete and ping the hooks this service owns."""

    async def create_webhook(
        self, repo_full_name: str, config: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """POST ``/repos/{repo}/hooks``."""
        ...

    async def list_webhooks(self, repo_full_name: str) -> list[Mapping[str, Any]]:
        """GET ``/repos/{repo}/hooks``."""
        ...

    async def delete_webhook(self, repo_full_name: str, hook_id: int) -> None:
        """DELETE ``/repos/{repo}/hooks/{hook_id}``."""
        ...

    async def ping_webhook(self, repo_full_name: str, hook_id: int) -> None:
        """POST ``/repos/{repo}/hooks/{hook_id}/pings``."""
        ...


@runtime_checkable
class CredentialProbe(Protocol):
    """Ask GitHub what a token is, and what it may do."""

    async def verify_token(self, token: str, probe_repo: str | None = None) -> Mapping[str, Any]:
        """Probe ``/user`` and ``/user/repos``."""
        ...

    async def probe_actions_write(self, repo_full_name: str) -> bool:
        """Whether the credential may dispatch in ``repo_full_name``."""
        ...


__all__ = [
    "CredentialProbe",
    "RepositoryReader",
    "RunControl",
    "RunReader",
    "WebhookManager",
]
