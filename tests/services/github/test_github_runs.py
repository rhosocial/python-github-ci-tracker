# tests/services/github/test_github_runs.py
"""``GithubRuns``: which endpoint is called, with which token, and what comes back.

The group does not interpret a payload -- the model does. So the assertions here
are about the *call*: the right repository, the right id, the filter that was
asked for, and the token the process decided on at construction.
"""

from __future__ import annotations

import pytest

from rhosocial.github_ci_tracker.services.github import GithubRuns

REPO = "acme/demo"


def _run_payload(**overrides: object) -> dict:
    payload = {
        "id": 90_001,
        "run_number": 12,
        "run_attempt": 2,
        "status": "in_progress",
        "conclusion": None,
        "workflow_id": 7_001,
    }
    payload.update(overrides)
    return payload


class TestReads:
    async def test_returns_the_payload_unchanged(self, read_transport) -> None:
        payload = _run_payload()
        read_transport.run_payload = payload

        result = await GithubRuns(read_transport).get_run(REPO, 90_001)

        # The same object, not a copy with fields picked out: the model reads it.
        assert result is payload

    async def test_jobs_come_back_as_the_payloads(self, read_transport) -> None:
        read_transport.job_payloads = [{"id": 80_001, "name": "tests"}]

        jobs = await GithubRuns(read_transport).get_run_jobs(REPO, 90_001)

        assert jobs == [{"id": 80_001, "name": "tests"}]

    async def test_the_log_is_the_body_verbatim(self, read_transport) -> None:
        read_transport.log_text = "line one\nline two\n"

        text = await GithubRuns(read_transport).get_job_logs(REPO, 80_001)

        assert text == "line one\nline two\n"

    async def test_list_forwards_the_filters_and_the_page(self, read_transport) -> None:
        read_transport.run_payloads = [_run_payload(id=1)]

        await GithubRuns(read_transport).list_runs(
            REPO,
            workflow_id="ci.yml",
            status="completed",
            branch="main",
            head_sha="abc123",
            per_page=10,
            page=3,
        )

        assert read_transport.calls == [
            (
                "list_runs",
                {
                    "token": None,
                    "repo": REPO,
                    "workflow_id": "ci.yml",
                    "status": "completed",
                    "branch": "main",
                    "head_sha": "abc123",
                    "per_page": 10,
                    "page": 3,
                },
            )
        ]

    async def test_the_ref_head_is_read_by_name(self, read_transport) -> None:
        """``if_absent`` needs "which commit is this ref now" as a separate read."""
        read_transport.ref_sha = "abc123"

        sha = await GithubRuns(read_transport).get_ref_sha(REPO, "main")

        assert sha == "abc123"
        assert read_transport.calls == [
            ("get_ref_sha", {"token": None, "repo": REPO, "ref": "main"})
        ]


class TestTokenSelection:
    async def test_ambient_credential_is_used(self, read_transport, credential) -> None:
        runs = GithubRuns(read_transport, credential=credential)

        await runs.get_run(REPO, 90_001)

        assert read_transport.calls == [
            ("get_run", {"token": "ghp_test_token", "repo": REPO, "run_id": 90_001})
        ]

    async def test_no_credential_reads_anonymously(self, read_transport) -> None:
        await GithubRuns(read_transport).get_run(REPO, 90_001)

        assert read_transport.calls[0][1]["token"] is None

    async def test_resolver_decides_per_repository(self, read_transport) -> None:
        asked: list[str] = []

        async def resolve(repo_full_name: str):
            asked.append(repo_full_name)

            class _Bound:
                token = "ghp_bound"

            return _Bound()

        await GithubRuns(read_transport, resolve=resolve).get_run(REPO, 90_001)

        assert asked == [REPO]
        assert read_transport.calls[0][1]["token"] == "ghp_bound"

    async def test_ambient_credential_wins_over_the_resolver(
        self, read_transport, credential
    ) -> None:
        called: list[str] = []

        async def resolve(repo_full_name: str):
            called.append(repo_full_name)
            return None

        runs = GithubRuns(read_transport, credential=credential, resolve=resolve)
        await runs.get_run(REPO, 90_001)

        assert called == []
        assert read_transport.calls[0][1]["token"] == "ghp_test_token"


class TestDispatch:
    async def test_forwards_the_request_and_returns_the_answer(self, read_transport) -> None:
        read_transport.dispatch_payload = {"run_id": 90_002, "status": "queued"}

        answer = await GithubRuns(read_transport).dispatch(REPO, "ci.yml", "main", {"k": "v"})

        assert answer == {"run_id": 90_002, "status": "queued"}
        assert read_transport.calls == [
            (
                "trigger_workflow",
                {
                    "token": None,
                    "repo": REPO,
                    "workflow_id": "ci.yml",
                    "ref": "main",
                    "inputs": {"k": "v"},
                },
            )
        ]

    async def test_a_204_is_reported_as_run_id_zero_not_an_error(self, read_transport) -> None:
        # GitHub Enterprise Server ignores the request for run details and answers
        # 204; the transport reports that as ``run_id 0``, and the caller reads the
        # run back instead of guessing.
        read_transport.dispatch_payload = {"run_id": 0, "status": "queued"}

        answer = await GithubRuns(read_transport).dispatch(REPO, "ci.yml", "main")

        assert answer["run_id"] == 0


class TestRerunAndCancel:
    async def test_rerun_targets_the_run(self, read_transport, credential) -> None:
        await GithubRuns(read_transport, credential=credential).rerun(REPO, 90_001)

        assert read_transport.calls == [
            ("rerun_workflow", {"token": "ghp_test_token", "repo": REPO, "run_id": 90_001})
        ]

    async def test_cancel_targets_the_run(self, read_transport) -> None:
        await GithubRuns(read_transport).cancel(REPO, 90_001)

        assert read_transport.calls == [
            ("cancel_workflow_run", {"token": None, "repo": REPO, "run_id": 90_001})
        ]


class TestErrors:
    async def test_a_transport_error_propagates_unchanged(self, read_transport) -> None:
        read_transport.error = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await GithubRuns(read_transport).get_run(REPO, 1)
