# tests/models/test_repository.py
"""AsyncRepository: subscribe / unsubscribe / anonymous mode / resolve_token.

A subscription registers ``owner/repo``, caches the repository metadata plus its
workflow list, and records which credential answers for it. ``resolve_token`` is
the routing rule every GitHub access goes through; its states are pinned here:
anonymous (``None``), healthy, deactivated, expired -- plus the dangling
reference case, where a binding points at a credential that no longer exists.

The earlier xfail note about the async model awaiting the sync twin is gone: the
async twin now resolves through the async provider, so there is no sync call on
that path to deadlock on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakeGithub, make_repository_service

from rhosocial.github_ci_tracker.core.exceptions import (
    RepositoryNotFoundError,
    TokenDeactivatedError,
    TokenExpiredError,
    TokenNotFoundError,
    TokenRequiredError,
    ValidationError,
)
from rhosocial.github_ci_tracker.credentials import AsyncPatCredential, get_provider
from rhosocial.github_ci_tracker.credentials.resolver import (
    require_for_repository,
    resolve_for_repository,
)
from rhosocial.github_ci_tracker.models import (
    AsyncJob,
    AsyncRepository,
    AsyncWorkflowRun,
)
from rhosocial.github_ci_tracker.models.binding import AsyncRepositoryBinding

#: A year back: an expired credential, computed rather than dated so it cannot
#: drift into the future and stop testing expiry at all.
PAST = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=365)


def _provider():
    """The PAT provider the service uses."""
    return get_provider("pat")


async def _credential(
    client: FakeGithub,
    token: str = "ghp_one",
    name: str = "laptop",
    expires_at: datetime | None = None,
) -> AsyncPatCredential:
    """Register a credential through the provider."""
    return await _provider().add(token=token, name=name, client=client, expires_at=expires_at)


async def _subscribe(
    client: FakeGithub,
    full_name: str,
    credential: AsyncPatCredential | None = None,
    **kwargs,
) -> AsyncRepository:
    """Subscribe through the service, binding a credential when given."""
    service = make_repository_service(client)
    return await service.subscribe_repository(
        full_name,
        token_id=credential.id if credential else None,
        **kwargs,
    )


def _public_repo_info() -> dict:
    """A repository payload that anonymous reads can see."""
    return {
        "id": 42,
        "owner": {"login": "acme"},
        "name": "public",
        "full_name": "acme/public",
        "is_private": False,
        "default_branch": "main",
    }


class TestAnonymousSubscription:
    """A repository with no binding reads anonymously."""

    async def test_anonymous_subscription_caches_metadata(
        self, fake_client: FakeGithub, bound
    ) -> None:
        fake_client.repo_info = _public_repo_info()
        repo = await _subscribe(fake_client, "acme/public")

        assert repo.id is not None
        assert repo.github_repo_id == 42
        assert repo.owner == "acme"
        assert repo.default_branch == "main"
        assert repo.is_private is False
        # No binding row is the definition of anonymous.
        assert await AsyncRepositoryBinding.for_repository(repo.id or 0) is None

    async def test_anonymous_subscription_is_idempotent(
        self, fake_client: FakeGithub, bound
    ) -> None:
        fake_client.repo_info = _public_repo_info()
        first = await _subscribe(fake_client, "acme/public")
        again = await _subscribe(fake_client, "acme/public")
        assert again.id == first.id

    async def test_anonymous_subscription_refuses_private_repositories(
        self, fake_client: FakeGithub, bound
    ) -> None:
        with pytest.raises(RepositoryNotFoundError, match="not publicly readable"):
            await _subscribe(fake_client, "acme/demo")

    async def test_malformed_names_are_rejected(self, fake_client: FakeGithub, bound) -> None:
        with pytest.raises(ValidationError):
            await _subscribe(fake_client, "")
        with pytest.raises(ValidationError):
            await _subscribe(fake_client, "no-slash")

    async def test_webhook_creation_requires_a_bound_credential(
        self, fake_client: FakeGithub, bound
    ) -> None:
        fake_client.repo_info = _public_repo_info()
        with pytest.raises(ValidationError, match="Webhook creation"):
            await _subscribe(
                fake_client,
                "acme/public",
                create_webhook=True,
                webhook_url="https://x.test/hook",
            )


class TestBoundSubscription:
    """A repository bound to a credential."""

    async def test_binds_a_credential(self, fake_client: FakeGithub, bound) -> None:
        laptop = await _credential(fake_client, "ghp_one", "laptop")
        repo = await _subscribe(fake_client, "acme/demo", laptop)

        binding = await AsyncRepositoryBinding.for_repository(repo.id or 0)
        assert binding is not None
        assert binding.provider_kind == "pat"
        assert binding.provider_ref == laptop.id

        again = await _subscribe(fake_client, "acme/demo", laptop)
        assert again.id == repo.id  # idempotent: one row, one binding

    async def test_rebinding_replaces_the_pair(self, fake_client: FakeGithub, bound) -> None:
        laptop = await _credential(fake_client, "ghp_one", "laptop")
        phone = await _credential(fake_client, "ghp_two", "phone")
        repo = await _subscribe(fake_client, "acme/demo", laptop)

        rebound = await AsyncRepositoryBinding.bind(repo.id or 0, "pat", phone.id or 0)
        assert rebound.repository_id == repo.id

        binding = await AsyncRepositoryBinding.for_repository(repo.id or 0)
        assert binding is not None
        assert binding.provider_ref == phone.id

    async def test_credential_failures_are_rejected(self, fake_client: FakeGithub, bound) -> None:
        with pytest.raises(TokenNotFoundError):
            await _provider().resolve(4242)

        ghost = await _credential(fake_client, "ghp_dead", "ghost")
        await _provider().deactivate(ghost.id or 0)
        with pytest.raises(TokenDeactivatedError):
            await _provider().resolve(ghost.id or 0)

        ci = await _credential(fake_client, "ghp_ci", "ci")
        fake_client.inaccessible_repos = frozenset({"acme/secret"})
        with pytest.raises(RepositoryNotFoundError, match="not accessible"):
            await _subscribe(fake_client, "acme/secret", ci)

    async def test_creates_webhook_for_bound_credential(
        self, fake_client: FakeGithub, bound
    ) -> None:
        credential = await _credential(fake_client)
        await _subscribe(
            fake_client,
            "acme/demo",
            credential,
            create_webhook=True,
            webhook_url="https://x.test/hook",
            webhook_secret="s3cret",
        )
        assert len(fake_client.created_hooks) == 1


class TestUnsubscribe:
    """Teardown drops the cached CI data and the binding."""

    async def test_purges_cached_data(self, fake_client: FakeGithub, bound) -> None:
        fake_client.repo_info = _public_repo_info()
        repo = await _subscribe(fake_client, "acme/public")
        run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload())
        await AsyncJob.upsert_from_remote(run, _job_payload())

        await repo.unsubscribe()

        assert await AsyncRepository.find_by_full_name("acme/public") is None
        assert await AsyncWorkflowRun.query().count() == 0
        assert await AsyncJob.query().count() == 0

    async def test_releases_the_credential_for_deletion(
        self, fake_client: FakeGithub, bound
    ) -> None:
        credential = await _credential(fake_client)
        repo = await _subscribe(fake_client, "acme/demo", credential)

        await repo.unsubscribe()
        # The binding is gone with the repository, so deletion may proceed.
        await _provider().delete(credential.id or 0)
        assert await AsyncPatCredential.find_by_name("laptop") is None


class TestResolveForRepository:
    """The four resolution states, and the dangling-reference case."""

    async def test_resolve_token_returns_none(self, fake_client: FakeGithub, bound) -> None:
        fake_client.repo_info = _public_repo_info()
        repo = await _subscribe(fake_client, "acme/public")
        assert await resolve_for_repository(repo) is None

    async def test_require_token_refuses_anonymous_mode(
        self, fake_client: FakeGithub, bound
    ) -> None:
        fake_client.repo_info = _public_repo_info()
        repo = await _subscribe(fake_client, "acme/public")
        with pytest.raises(TokenRequiredError):
            await require_for_repository(repo)

    async def test_healthy_binding_returns_the_credential(
        self, fake_client: FakeGithub, bound
    ) -> None:
        credential = await _credential(fake_client, token="ghp_secret_value")
        repo = await _subscribe(fake_client, "acme/demo", credential)

        resolved = await resolve_for_repository(repo)
        assert resolved is not None
        assert resolved.token == "ghp_secret_value"
        assert resolved.identity == "alice"

    async def test_deactivated_binding_raises(self, fake_client: FakeGithub, bound) -> None:
        credential = await _credential(fake_client)
        repo = await _subscribe(fake_client, "acme/demo", credential)
        await _provider().deactivate(credential.id or 0)

        with pytest.raises(TokenDeactivatedError):
            await resolve_for_repository(repo)

    async def test_expired_binding_raises(self, fake_client: FakeGithub, bound) -> None:
        """An expired credential refuses resolution.

        The binding row is written directly instead of going through
        ``subscribe``: subscribing parses the repository from GitHub, which
        needs the credential, so it would raise here rather than on the later
        ``resolve_token`` the test is about.
        """
        credential = await _credential(fake_client, expires_at=PAST)
        repo = AsyncRepository(
            github_repo_id=7,
            full_name="acme/demo",
            owner="acme",
            name="demo",
        )
        await repo.save()
        await AsyncRepositoryBinding.bind(repo.id or 0, "pat", credential.id or 0)

        with pytest.raises(TokenExpiredError):
            await resolve_for_repository(repo)

    async def test_dangling_binding_raises(self, fake_client: FakeGithub, bound) -> None:
        """A binding whose credential vanished must not degrade to anonymous."""
        credential = await _credential(fake_client)
        repo = await _subscribe(fake_client, "acme/demo", credential)

        # Force-delete the row behind the binding's back.
        stored = await AsyncPatCredential.find_one(AsyncPatCredential.c.id == credential.id)
        assert stored is not None
        await stored.delete()

        with pytest.raises(TokenNotFoundError, match="does not exist"):
            await resolve_for_repository(repo)


def _run_payload(github_run_id: int = 500) -> dict:
    """A minimal run payload."""
    return {
        "id": github_run_id,
        "run_number": 1,
        "run_attempt": 1,
        "event": "push",
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


def _job_payload() -> dict:
    """A minimal job payload."""
    return {
        "id": 900,
        "run_id": 500,
        "run_attempt": 1,
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
            }
        ],
    }


class TestPullRequestRuns:
    """RepositoryController.pull_request_runs: PR -> head commit -> runs.

    GitHub has no PR-number filter on the runs endpoint, so the service reads the
    pull request for its head commit and then attributes the runs of that commit
    through the payload's ``pull_requests`` array (authoritative while the pull
    request is open) or, failing that, through ``event`` + ``head_sha``.

    This orchestration lives in the service, not the model: which runs are worth
    storing and whether to spend a second call on their jobs is a fetch-then-store
    decision, and the model only stores payloads it is handed.
    """

    @staticmethod
    def _pr_run(
        github_run_id: int,
        *,
        head_sha: str = "pr-sha",
        event: str = "pull_request",
        pull_requests: list[dict[str, int]] | None = None,
    ) -> dict[str, object]:
        """A run payload shaped like the pull-request runs endpoint returns."""
        payload = dict(_run_payload(github_run_id))
        payload.update(
            {
                "head_sha": head_sha,
                "event": event,
                "pull_requests": pull_requests if pull_requests is not None else [],
            }
        )
        return payload

    @staticmethod
    def _open_pr(number: int, head_sha: str = "pr-sha") -> dict[str, object]:
        """A pull-request payload carrying the head commit the runs share."""
        return {"number": number, "state": "open", "head": {"sha": head_sha}}

    async def test_queries_the_pr_then_stores_its_runs(
        self, fake_client: FakeGithub, bound
    ) -> None:
        fake_client.repo_info = _public_repo_info()
        fake_client.pull_requests[7] = self._open_pr(7)
        fake_client.runs = [
            self._pr_run(501, pull_requests=[{"number": 7}]),
            self._pr_run(502, pull_requests=[{"number": 7}]),
        ]
        fake_client.jobs = [
            {
                "id": 901,
                "run_id": 501,
                "name": "build",
                "status": "completed",
                "conclusion": "success",
                "steps": [
                    {
                        "number": 1,
                        "name": "checkout",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ],
            }
        ]
        service = make_repository_service(fake_client)
        await _subscribe(fake_client, "acme/public")

        runs = await service.pull_request_runs("acme/public", 7)

        assert {record.run_id for record in runs} == {501, 502}
        # Both runs are persisted, not just returned.
        assert await AsyncWorkflowRun.query().count() == 2
        stored = await AsyncWorkflowRun.find_by_github_id(501)
        assert stored is not None
        # The refresh phase synced jobs and steps for the matched runs.
        jobs = [job for attempt in await stored.attempts() for job in await attempt.jobs()]
        assert [job.name for job in jobs] == ["build"]
        assert [step.name for step in await jobs[0].steps()] == ["checkout"]

    async def test_unrelated_runs_are_not_attributed(self, fake_client: FakeGithub, bound) -> None:
        fake_client.repo_info = _public_repo_info()
        fake_client.pull_requests[7] = self._open_pr(7)
        fake_client.runs = [
            # Another pull request that happens to share the head commit: the
            # authoritative array says #8, so #7 must not claim it.
            self._pr_run(501, pull_requests=[{"number": 8}]),
            # A push run on the same commit is a different event.
            self._pr_run(502, event="push", pull_requests=[]),
            # A pull_request run on a different commit.
            self._pr_run(503, head_sha="other-sha", pull_requests=[{"number": 7}]),
        ]
        service = make_repository_service(fake_client)
        await _subscribe(fake_client, "acme/public")

        assert await service.pull_request_runs("acme/public", 7) == []
        assert await AsyncWorkflowRun.query().count() == 0

    async def test_closed_pr_falls_back_to_event_and_head_sha(
        self, fake_client: FakeGithub, bound
    ) -> None:
        # GitHub drops the ``pull_requests`` attribution once a PR closes, so a
        # closed PR is still recovered through event + head_sha.
        fake_client.repo_info = _public_repo_info()
        fake_client.pull_requests[7] = self._open_pr(7, head_sha="closed-sha")
        fake_client.runs = [self._pr_run(501, head_sha="closed-sha", pull_requests=[])]
        service = make_repository_service(fake_client)
        await _subscribe(fake_client, "acme/public")

        runs = await service.pull_request_runs("acme/public", 7)

        assert [record.run_id for record in runs] == [501]

    async def test_empty_result_is_an_answer_not_an_error(
        self, fake_client: FakeGithub, bound
    ) -> None:
        fake_client.repo_info = _public_repo_info()
        fake_client.pull_requests[7] = self._open_pr(7, head_sha="never-ran")
        fake_client.runs = []
        service = make_repository_service(fake_client)
        await _subscribe(fake_client, "acme/public")

        assert await service.pull_request_runs("acme/public", 7) == []

    async def test_unknown_pull_request_raises(self, fake_client: FakeGithub, bound) -> None:
        fake_client.repo_info = _public_repo_info()
        service = make_repository_service(fake_client)
        await _subscribe(fake_client, "acme/public")

        with pytest.raises(RepositoryNotFoundError, match="Pull request #7"):
            await service.pull_request_runs("acme/public", 7)

    async def test_sync_jobs_false_stores_the_run_without_fetching_jobs(
        self, fake_client: FakeGithub, bound
    ) -> None:
        fake_client.repo_info = _public_repo_info()
        fake_client.pull_requests[7] = self._open_pr(7)
        fake_client.runs = [self._pr_run(501, pull_requests=[{"number": 7}])]
        fake_client.jobs = [{"id": 901, "run_id": 501, "name": "build", "status": "completed"}]
        service = make_repository_service(fake_client)
        await _subscribe(fake_client, "acme/public")

        runs = await service.pull_request_runs("acme/public", 7, sync_jobs=False)

        assert [record.run_id for record in runs] == [501]
        stored = await AsyncWorkflowRun.find_by_github_id(501)
        assert stored is not None
        assert len([job for a in await stored.attempts() for job in await a.jobs()]) == 0
