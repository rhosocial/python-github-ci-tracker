# tests/core/test_facade_views.py
"""Facade run views must carry their jobs and their cache facts.

The view layer once built ``RunRecord`` without ever attaching jobs, so every
run endpoint reported ``jobs: []`` even though the rows were stored. These
tests pin the fixed behavior: a run view includes the jobs of that run, and
listing several runs keeps each run's jobs separate (the batch grouping).

They also pin the two attempt numbers a run view reports, which are deliberately
different facts: ``attempts_total`` is what GitHub said (the run row's
``run_attempt``) and ``attempts_cached`` is what the local attempt table holds.
Reporting the second under the first's name answered "how many retries did this
run get?" with "how many did we happen to store".
"""

from __future__ import annotations

from typing import Any

from conftest import FakeGithub, subscribe

from rhosocial.github_ci_tracker.controllers import QueryController, RunController, RunData
from rhosocial.github_ci_tracker.models import AsyncJob, AsyncRepository, AsyncWorkflowRun
from rhosocial.github_ci_tracker.services.github import GithubRepositories, GithubRuns


def _query(fake: FakeGithub | None = None) -> QueryController:
    """A query service over the local database, with GitHub faked."""
    client = fake or FakeGithub()
    runs = GithubRuns(client)
    store = RunData()
    return QueryController(
        readers=runs,
        repositories=GithubRepositories(client),
        runs=RunController(runs=runs, runs_data=store),
        runs_data=store,
    )


def _public_repo_info() -> dict[str, Any]:
    """Repository metadata of a publicly readable repository."""
    return {
        "id": 42,
        "owner": {"login": "acme"},
        "name": "public",
        "full_name": "acme/public",
        "is_private": False,
        "default_branch": "trunk",
    }


def _run_payload(
    github_run_id: int,
    *,
    attempt: int = 1,
    status: str = "completed",
) -> dict[str, Any]:
    """A GitHub run payload, finished unless ``status`` says otherwise."""
    return {
        "id": github_run_id,
        "run_number": github_run_id % 1000,
        "run_attempt": attempt,
        "event": "push",
        "status": status,
        "conclusion": "success" if status == "completed" else None,
        "head_branch": "main",
        "head_sha": "abc",
        "workflow_id": 7,
        "path": ".github/workflows/ci.yml",
        "name": "CI",
        "run_started_at": "2026-10-10T10:00:00Z",
        "completed_at": "2026-10-10T10:05:00Z" if status == "completed" else None,
    }


def _job_payload(github_job_id: int, name: str) -> dict[str, Any]:
    """A completed GitHub job payload."""
    return {
        "id": github_job_id,
        "name": name,
        "status": "completed",
        "conclusion": "success",
        "runner_name": "ubuntu-latest",
        "started_at": "2026-10-10T10:00:00Z",
        "completed_at": "2026-10-10T10:05:00Z",
    }


async def _seed_run(
    repo: AsyncRepository,
    github_run_id: int,
    job_names: list[str],
    *,
    attempt: int = 1,
    status: str = "completed",
) -> AsyncWorkflowRun:
    """Store a run with one job per given name."""
    run = await AsyncWorkflowRun.adopt_from_remote(
        repo, _run_payload(github_run_id, attempt=attempt, status=status)
    )
    for offset, name in enumerate(job_names):
        await AsyncJob.upsert_from_remote(run, _job_payload(github_run_id * 10 + offset, name))
    return run


class TestRunViewJobs:
    async def test_single_run_view_includes_its_jobs(self, fake_client: FakeGithub, bound) -> None:
        fake_client.repo_info = _public_repo_info()
        repo = await subscribe(fake_client)
        await _seed_run(repo, 501, ["build", "test"])
        fake_client.runs = [_run_payload(501)]

        record = await _query(fake_client).get_run_status_by_id(501)

        assert [job.name for job in record.jobs] == ["build", "test"]
        assert all(job.run_id == 501 for job in record.jobs)

    async def test_a_list_is_a_summary_and_does_not_load_jobs(
        self, fake_client: FakeGithub, bound
    ) -> None:
        """Jobs belong to ``get_run``; a list that loaded them paid for steps
        nobody read and still could not answer "which job went red" cheaply."""
        fake_client.repo_info = _public_repo_info()
        repo = await subscribe(fake_client)
        await _seed_run(repo, 501, ["build"])
        await _seed_run(repo, 502, ["test", "package"])
        fake_client.runs = [_run_payload(502), _run_payload(501)]

        records = await _query(fake_client).list_runs(repository="acme/public")

        assert [record.run_id for record in records] == [502, 501]
        assert all(record.jobs == [] for record in records)
        detail = await _query(fake_client).get_run_status_by_id(501)
        assert [job.name for job in detail.jobs] == ["build"]

    async def test_run_without_jobs_reports_an_empty_list(
        self, fake_client: FakeGithub, bound
    ) -> None:
        fake_client.repo_info = _public_repo_info()
        repo = await subscribe(fake_client)
        await _seed_run(repo, 501, [])
        fake_client.runs = [_run_payload(501)]

        record = await _query(fake_client).get_run_status_by_id(501)

        assert record.jobs == []


class TestRunCacheFacts:
    """How complete the local copy of a run is, and where the numbers come from."""

    async def test_a_finished_run_everything_was_synced_for_is_cached(
        self, fake_client: FakeGithub, bound
    ) -> None:
        fake_client.repo_info = _public_repo_info()
        repo = await subscribe(fake_client)
        await _seed_run(repo, 601, ["build", "test"])
        fake_client.runs = [_run_payload(601)]

        record = await _query(fake_client).get_run_status_by_id(601)

        assert record.attempts_total == 1
        assert record.attempts_cached == 1
        assert record.cache_state == "cached"

    async def test_a_newer_attempt_reported_by_github_is_stored(
        self, fake_client: FakeGithub, bound
    ) -> None:
        """GitHub is the source: an attempt we have not seen is written, not
        reported as a gap between "what GitHub says" and "what we cached"."""
        fake_client.repo_info = _public_repo_info()
        repo = await subscribe(fake_client)
        await _seed_run(repo, 602, ["build"])
        fake_client.runs = [_run_payload(602, attempt=2, status="in_progress")]

        record = await _query(fake_client).get_run_status_by_id(602)

        assert record.attempts_total == 2
        assert record.attempts_cached == 2
        assert record.attempt == 2

    async def test_a_live_run_is_stale_even_when_nothing_is_missing(
        self, fake_client: FakeGithub, bound
    ) -> None:
        fake_client.repo_info = _public_repo_info()
        repo = await subscribe(fake_client)
        await _seed_run(repo, 603, ["build"], status="in_progress")
        fake_client.runs = [_run_payload(603, status="in_progress")]

        record = await _query(fake_client).get_run_status_by_id(603)

        assert record.attempts_cached == 1
        assert record.cache_state == "stale"

    async def test_an_attempt_without_its_jobs_is_partial(
        self, fake_client: FakeGithub, bound
    ) -> None:
        """A run stored without its jobs is an incomplete cache."""
        fake_client.repo_info = _public_repo_info()
        repo = await subscribe(fake_client)
        await _seed_run(repo, 604, [], status="completed")
        fake_client.runs = [_run_payload(604)]

        record = await _query(fake_client).get_run_status_by_id(604)

        assert record.attempts_cached == 1
        assert record.jobs == []
        assert record.cache_state == "partial"

    async def test_summaries_of_several_repositories_keep_their_facts(
        self, fake_client: FakeGithub, bound
    ) -> None:
        """The batch path computes the facts in grouped queries, not per run."""
        fake_client.repo_info = _public_repo_info()
        repo = await subscribe(fake_client)
        await _seed_run(repo, 605, ["build"])
        await _seed_run(repo, 606, ["build"], status="in_progress")
        fake_client.runs = [
            _run_payload(606, status="in_progress"),
            _run_payload(605),
        ]

        entries = await _query(fake_client).list_run_summaries(["acme/public"])

        assert [entry.repository for entry in entries] == ["acme/public"]
        assert [(entry.run_id, entry.cache_state) for entry in entries[0].runs] == [
            (606, "stale"),
            (605, "cached"),
        ]
