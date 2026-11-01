# src/rhosocial/github_ci_tracker/services/github/runs.py
"""Runs: read them, and ask GitHub to run or stop them.

Both halves live in one object because they are the same resource -- ``/actions/
runs`` -- but a caller is typed against the protocol it actually needs, so a
reader cannot dispatch by accident: :class:`~.protocols.RunReader` and
:class:`~.protocols.RunControl` are separate.

Payloads go back unchanged. The model knows what a run payload means
(``adopt_from_remote``, ``update_from_remote``, ``sync_jobs_from_remote``), and a
second reader here would be a copy that drifts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .base import TokenSource
from .client import AsyncGithubClient
from .logs import RunLogArchive


def _default_client() -> AsyncGithubClient:
    """The HTTP client, built once per group."""
    return AsyncGithubClient()


class GithubRuns:
    """``/actions/runs``: reads, and the three verbs that spend CI minutes.

    No ``token`` parameter anywhere: the credential is decided once, in
    :class:`~.base.TokenSource`. Threading it through every call is what made the
    old controllers resolve the same credential per request, and made a caller
    that forgot read anonymously without saying so.
    """

    def __init__(
        self,
        client: AsyncGithubClient | None = None,
        *,
        credential: Any | None = None,
        resolve: Callable[[str], Awaitable[Any | None]] | None = None,
    ) -> None:
        """Build the group.

        Args:
            client: Injected transport; a default HTTP client is built when
                omitted, which is what every caller outside a test does.
            credential: The ambient credential, when the process has one.
            resolve: Async callable ``repo_full_name -> credential | None`` used
                when there is no ambient credential.
        """
        self._client = client if client is not None else _default_client()
        self._tokens = TokenSource(credential=credential, resolve=resolve)

    # -- reads ----------------------------------------------------------------

    async def get_run(self, repo_full_name: str, run_id: int) -> Mapping[str, Any]:
        """Fetch one run.

        Raises:
            GithubAPIError: GitHub refused the read; a 404 arrives here too.
        """
        return await self._client.get_run(
            token=await self._tokens.token_for(repo_full_name),
            repo_full_name=repo_full_name,
            run_id=run_id,
        )

    async def get_run_attempt(
        self, repo_full_name: str, run_id: int, attempt: int
    ) -> Mapping[str, Any]:
        """Fetch one attempt of a run.

        Raises:
            GithubAPIError: GitHub refused the read; an attempt past the last one
                answers 404.
        """
        return await self._client.get_run_attempt(
            token=await self._tokens.token_for(repo_full_name),
            repo_full_name=repo_full_name,
            run_id=run_id,
            attempt=attempt,
        )

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
        """List runs, newest first."""
        return await self._client.list_runs(
            token=await self._tokens.token_for(repo_full_name),
            repo_full_name=repo_full_name,
            workflow_id=workflow_id,
            status=status,
            branch=branch,
            head_sha=head_sha,
            per_page=per_page,
            page=page,
        )

    async def get_ref_sha(self, repo_full_name: str, ref: str) -> str:
        """The commit ``ref`` points at right now."""
        return await self._client.get_ref_sha(
            token=await self._tokens.token_for(repo_full_name),
            repo_full_name=repo_full_name,
            ref=ref,
        )

    async def get_run_jobs(self, repo_full_name: str, run_id: int) -> list[Mapping[str, Any]]:
        """Fetch the jobs of a run's latest attempt, steps included."""
        return await self._client.get_run_jobs(
            token=await self._tokens.token_for(repo_full_name),
            repo_full_name=repo_full_name,
            run_id=run_id,
        )

    async def get_run_logs(self, repo_full_name: str, run_id: int) -> RunLogArchive:
        """Download and parse a run's log archive.

        Parsed here rather than at the caller because the archive's layout is
        GitHub's, not ours: the controller should receive "these step files, this
        merged text", not a zip and a set of rules that only GitHub's format
        justifies.

        Raises:
            GithubAPIError: GitHub refused the download, or the logs are already
                gone (the archive is kept for 90 days).
            zipfile.BadZipFile: The body is not a zip; the caller sees a
                transport failure rather than an empty archive.
        """
        raw = await self._client.get_run_logs(
            token=await self._tokens.token_for(repo_full_name),
            repo_full_name=repo_full_name,
            run_id=run_id,
        )
        return RunLogArchive.from_bytes(raw)

    async def get_job(self, repo_full_name: str, job_id: int) -> Mapping[str, Any]:
        """Fetch one job, steps included.

        Raises:
            GithubAPIError: GitHub refused the read; a job of a purged run
                answers 404.
        """
        return await self._client.get_job(
            token=await self._tokens.token_for(repo_full_name),
            repo_full_name=repo_full_name,
            job_id=job_id,
        )

    async def get_job_logs(self, repo_full_name: str, job_id: int) -> str:
        """Fetch a job's whole log -- one call, GitHub serves it as one body."""
        return await self._client.get_job_logs(
            token=await self._tokens.token_for(repo_full_name),
            repo_full_name=repo_full_name,
            job_id=job_id,
        )

    # -- writes ---------------------------------------------------------------

    async def dispatch(
        self,
        repo_full_name: str,
        workflow_id: str,
        ref: str,
        inputs: dict[str, str] | None = None,
    ) -> Mapping[str, Any]:
        """Dispatch a workflow on ``ref``.

        The answer is the transport's: ``{"run_id": 0, "status": "queued"}`` when
        GitHub answered ``204`` and created the run without naming it. ``0`` is
        the contract for "created, id unknown" -- the caller reads the run back
        rather than guessing, because listing recent runs races with any
        concurrent dispatch.

        Raises:
            GithubAPIError: GitHub refused the dispatch (a 422 on a bad ref, a 403
                on a forbidden workflow).
        """
        return await self._client.trigger_workflow(
            token=await self._tokens.token_for(repo_full_name),
            repo_full_name=repo_full_name,
            workflow_id=workflow_id,
            ref=ref,
            inputs=inputs,
        )

    async def rerun(self, repo_full_name: str, run_id: int) -> None:
        """Re-run a finished run.

        GitHub answers ``201`` with no body and reuses the id while incrementing
        ``run_attempt``, so the new attempt is discovered by reading the run again
        -- which is why this returns nothing.
        """
        await self._client.rerun_workflow(
            token=await self._tokens.token_for(repo_full_name),
            repo_full_name=repo_full_name,
            run_id=run_id,
        )

    async def cancel(self, repo_full_name: str, run_id: int) -> None:
        """Cancel a queued or in-progress run.

        GitHub answers ``202`` before the runner stops: the run reaches
        ``completed / cancelled`` a moment later, so a caller that wants to store
        the new state reads the run again instead of trusting this answer.
        """
        await self._client.cancel_workflow_run(
            token=await self._tokens.token_for(repo_full_name),
            repo_full_name=repo_full_name,
            run_id=run_id,
        )


__all__ = ["GithubRuns"]
