# tests/controllers/test_repository_controller.py
"""End to end for ``RepositoryController``: fake transport, real models, real database.

Subscribing is the sequence worth pinning, because the order is the design:
settle the credential, read the repository, store it, bind it, cache the
workflows, and only then arm the hook. Every step below is observable.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import FakeGithub

from rhosocial.github_ci_tracker.controllers import RepositoryData, RunData
from rhosocial.github_ci_tracker.controllers.query import QueryController
from rhosocial.github_ci_tracker.controllers.repository import RepositoryController
from rhosocial.github_ci_tracker.controllers.run import RunController
from rhosocial.github_ci_tracker.core.exceptions import RepositoryNotFoundError, ValidationError
from rhosocial.github_ci_tracker.models import AsyncRepository, AsyncWorkflow
from rhosocial.github_ci_tracker.models.binding import AsyncRepositoryBinding
from rhosocial.github_ci_tracker.services.github import (
    GithubRepositories,
    GithubRuns,
    GithubWebhooks,
)

REPO = "acme/demo"
RUN_ID = 500


def _credential(ref: int = 7) -> SimpleNamespace:
    """A resolved credential with the provider pair a binding stores."""
    return SimpleNamespace(token="ghp_test", kind="pat", ref=ref, identity="alice")


def _service(fake: FakeGithub, *, credential=None) -> RepositoryController:
    runs_group = GithubRuns(fake)
    runs_data = RunData()
    run_service = RunController(runs=runs_group, runs_data=runs_data)
    query = QueryController(
        readers=runs_group,
        repositories=GithubRepositories(fake),
        runs=run_service,
        runs_data=runs_data,
    )
    return RepositoryController(
        repositories=GithubRepositories(fake),
        webhooks=GithubWebhooks(fake),
        readers=runs_group,
        runs=run_service,
        data=RepositoryData(),
        runs_data=runs_data,
        query=query,
        credential=credential,
    )


class TestSubscribe:
    async def test_subscribing_stores_binds_and_caches_the_workflows(self, bound) -> None:
        fake = FakeGithub()

        repo = await _service(fake, credential=_credential()).subscribe_repository(REPO)

        assert repo.full_name == REPO
        # The repository row, its binding and the workflow cache all exist.
        assert await AsyncRepository.query().count() == 1
        bindings = await AsyncRepositoryBinding.query().all()
        assert [(row.provider_kind, row.provider_ref) for row in bindings] == [("pat", 7)]
        assert [item.path for item in await AsyncWorkflow.query().all()] == [
            ".github/workflows/ci.yml"
        ]

    async def test_subscribing_twice_returns_the_existing_row(self, bound) -> None:
        fake = FakeGithub()
        service = _service(fake, credential=_credential())
        await service.subscribe_repository(REPO)

        again = await service.subscribe_repository(REPO)

        assert again.full_name == REPO
        assert await AsyncRepository.query().count() == 1
        # Idempotent means it did not go back to GitHub for the metadata.
        assert fake.repo_info_calls == 1

    async def test_a_malformed_name_is_refused_before_github(self, bound) -> None:
        fake = FakeGithub()

        with pytest.raises(ValidationError):
            await _service(fake).subscribe_repository("acme")

        assert fake.repo_info_calls == 0

    async def test_webhooks_need_a_credential(self, bound) -> None:
        """An anonymous subscription cannot arm a hook: it has nothing to authenticate."""
        fake = FakeGithub()

        with pytest.raises(ValidationError):
            await _service(fake).subscribe_repository(REPO, create_webhook=True)

        assert fake.created_hooks == []

    async def test_an_invisible_private_repository_explains_itself(self, bound) -> None:
        fake = FakeGithub(inaccessible_repos={REPO})

        with pytest.raises(RepositoryNotFoundError) as caught:
            await _service(fake).subscribe_repository(REPO)

        assert "not publicly readable" in str(caught.value)


class TestUnsubscribe:
    async def test_unsubscribing_drops_the_rows_and_this_services_hooks(
        self, bound, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CITRACKER_WEBHOOK_URL", "https://ci.test/hooks")
        monkeypatch.setenv("CITRACKER_WEBHOOK_SECRET", "s3cret")
        fake = FakeGithub()
        service = _service(fake, credential=_credential())
        await service.subscribe_repository(REPO, create_webhook=True)
        assert len(fake.created_hooks) == 1

        await service.unsubscribe_repository(full_name=REPO)

        assert await AsyncRepository.query().count() == 0
        assert await AsyncWorkflow.query().count() == 0
        assert fake.deleted_hook_ids == [1]

    async def test_a_foreign_hook_is_left_alone(
        self, bound, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CITRACKER_WEBHOOK_URL", "https://ci.test/hooks")
        monkeypatch.setenv("CITRACKER_WEBHOOK_SECRET", "s3cret")
        fake = FakeGithub()
        fake.created_hooks.append({"id": 9, "config": {"url": "https://someone-else.example/h"}})
        service = _service(fake, credential=_credential())
        await service.subscribe_repository(REPO)

        await service.unsubscribe_repository(full_name=REPO)

        assert fake.deleted_hook_ids == []


class TestWorkflows:
    async def test_an_empty_cache_is_refreshed_once(self, bound) -> None:
        fake = FakeGithub()
        service = _service(fake, credential=_credential())
        await service.subscribe_repository(REPO)
        # A subscription whose workflow cache was lost.
        for row in await AsyncWorkflow.query().all():
            await row.delete()

        workflows = await service.list_workflows(REPO)

        assert [item.path for item in workflows] == [".github/workflows/ci.yml"]

    async def test_a_workflow_resolves_by_file_name(self, bound) -> None:
        fake = FakeGithub()
        service = _service(fake, credential=_credential())
        await service.subscribe_repository(REPO)

        workflow = await service.get_workflow(REPO, "ci.yml")

        assert workflow.name == "CI"


class TestPullRequestRuns:
    async def test_runs_are_attributed_by_head_commit(self, bound) -> None:
        fake = FakeGithub()
        fake.pull_requests[12] = {"number": 12, "head": {"sha": "abc"}}
        fake.runs[0] = {**fake.runs[0], "head_sha": "abc", "event": "pull_request"}
        service = _service(fake, credential=_credential())
        await service.subscribe_repository(REPO)

        records = await service.pull_request_runs(REPO, 12)

        assert [record.run_id for record in records] == [RUN_ID]

    async def test_a_missing_pull_request_is_a_not_found(self, bound) -> None:
        fake = FakeGithub()
        service = _service(fake, credential=_credential())
        await service.subscribe_repository(REPO)

        with pytest.raises(RepositoryNotFoundError):
            await service.pull_request_runs(REPO, 404)


class TestBindings:
    """Binding a credential to a subscription that already exists."""

    async def test_binding_replaces_the_credential_a_subscription_started_with(self, bound) -> None:
        fake = FakeGithub()
        service = _service(fake, credential=_credential(7))
        await service.subscribe_repository(REPO)

        await service.bind_credential(REPO, "pat", 9)

        bindings = await AsyncRepositoryBinding.query().all()
        assert [(row.provider_kind, row.provider_ref) for row in bindings] == [("pat", 9)]

    async def test_unbinding_reports_whether_there_was_a_binding(self, bound) -> None:
        fake = FakeGithub()
        service = _service(fake, credential=_credential(7))
        await service.subscribe_repository(REPO)

        assert await service.unbind_credential(REPO) is True
        assert await service.unbind_credential(REPO) is False
        assert await AsyncRepositoryBinding.query().count() == 0

    async def test_an_unsubscribed_repository_is_refused(self, bound) -> None:
        """Binding and unbinding must not invent a repository row."""
        service = _service(FakeGithub())

        with pytest.raises(RepositoryNotFoundError):
            await service.bind_credential("acme/ghost", "pat", 9)
        with pytest.raises(RepositoryNotFoundError):
            await service.unbind_credential("acme/ghost")

        assert await AsyncRepository.query().count() == 0

    async def test_a_binding_needs_a_provider_pair(self, bound) -> None:
        fake = FakeGithub()
        service = _service(fake, credential=_credential(7))
        await service.subscribe_repository(REPO)

        with pytest.raises(ValidationError):
            await service.bind_credential(REPO, "", 7)


class TestBindingSummary:
    async def test_bound_and_anonymous_subscriptions_are_counted_apart(self, bound) -> None:
        fake = FakeGithub()
        service = _service(fake, credential=_credential(7))
        await service.subscribe_repository(REPO)
        await AsyncRepository(
            github_repo_id=8,
            full_name="acme/other",
            owner="acme",
            name="other",
        ).save()

        summary = await service.binding_summary()

        assert (summary.bound, summary.anonymous) == (1, 1)


class TestWorkflowsOf:
    async def test_the_batch_reads_the_cache_first(self, bound) -> None:
        fake = FakeGithub()
        service = _service(fake, credential=_credential(7))
        await service.subscribe_repository(REPO)

        workflows = await service.workflows_of([REPO, "acme/ghost"])

        assert [workflow.path for workflow in workflows[REPO]] == [".github/workflows/ci.yml"]
        assert "acme/ghost" not in workflows, "an unsubscribed name is skipped"

    async def test_an_empty_cache_is_refreshed_from_github(self, bound) -> None:
        """An empty cache means "not synced yet", not "has no workflows"."""
        await AsyncRepository(
            github_repo_id=3,
            full_name="acme/fresh",
            owner="acme",
            name="fresh",
        ).save()
        service = _service(FakeGithub(), credential=_credential(7))

        workflows = await service.workflows_of(["acme/fresh"])

        assert [workflow.path for workflow in workflows["acme/fresh"]] == [
            ".github/workflows/ci.yml"
        ]
