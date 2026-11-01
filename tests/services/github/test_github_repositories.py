# tests/services/github/test_github_repositories.py
"""``GithubRepositories``: metadata, workflows, and the one call that takes a credential.

Payloads come back unchanged -- ``AsyncRepository.upsert_from_remote`` and
``AsyncWorkflow.upsert_from_remote`` read them -- so the assertions are about the
call, not about parsing.
"""

from __future__ import annotations

import pytest

from rhosocial.github_ci_tracker.core.exceptions import TokenRequiredError
from rhosocial.github_ci_tracker.services.github import GithubRepositories

REPO = "acme/demo"


class TestReads:
    async def test_repo_info_is_the_payload(self, read_transport) -> None:
        payload = {"id": 4_242, "full_name": REPO, "default_branch": "main"}
        read_transport.repo_payload = payload

        info = await GithubRepositories(read_transport).get_repo_info(REPO)

        assert info is payload

    async def test_workflows_come_back_as_the_payloads(self, read_transport) -> None:
        read_transport.workflow_payloads = [
            {"id": 7_001, "path": ".github/workflows/ci.yml", "state": "active"},
        ]

        workflows = await GithubRepositories(read_transport).get_repo_workflows(REPO)

        assert [row["id"] for row in workflows] == [7_001]
        assert read_transport.calls == [("get_repo_workflows", {"token": None, "repo": REPO})]

    async def test_ambient_credential_is_used(self, read_transport, credential) -> None:
        await GithubRepositories(read_transport, credential=credential).get_repo_info(REPO)

        assert read_transport.calls == [
            ("get_repo_info", {"token": "ghp_test_token", "repo": REPO})
        ]

    async def test_resolver_decides_per_repository(self, read_transport) -> None:
        asked: list[str] = []

        async def resolve(repo_full_name: str):
            asked.append(repo_full_name)

            class _Bound:
                token = "ghp_bound"

            return _Bound()

        await GithubRepositories(read_transport, resolve=resolve).get_repo_info(REPO)

        assert asked == [REPO]
        assert read_transport.calls[0][1]["token"] == "ghp_bound"


class TestAccessible:
    async def test_the_given_credential_is_the_one_asked_about(
        self, read_transport, credential
    ) -> None:
        class _Other:
            token = "ghp_explicit"

        # The ambient credential is deliberately different: the explicit one must
        # win, because "what can this token see" is a question about that token.
        await GithubRepositories(read_transport, credential=credential).list_accessible_repos(
            _Other()
        )

        assert read_transport.calls == [("list_accessible_repos", {"token": "ghp_explicit"})]

    async def test_without_an_argument_the_ambient_one_is_used(
        self, read_transport, credential
    ) -> None:
        await GithubRepositories(read_transport, credential=credential).list_accessible_repos()

        assert read_transport.calls == [("list_accessible_repos", {"token": "ghp_test_token"})]

    async def test_no_credential_at_all_is_an_error_not_an_anonymous_call(
        self, read_transport
    ) -> None:
        with pytest.raises(TokenRequiredError):
            await GithubRepositories(read_transport).list_accessible_repos()

        # It must not have reached the transport: an anonymous /user/repos would
        # answer 401 and read as a GitHub failure rather than a caller mistake.
        assert read_transport.calls == []
