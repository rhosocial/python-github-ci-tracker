# src/rhosocial/github_ci_tracker/controllers/repository.py
"""The repository controller: subscription flows, and the rows behind them.

A controller is a group of flows. :class:`RepositoryController` decides *when*
to subscribe, refresh or unsubscribe, and settles which credential answers;
:class:`RepositoryData` below decides *how* that lands in the database.

Making a subscription is a fetch-then-store sequence, and the order is the
point: store the repository, record the binding, cache the workflow list, and
only then arm the webhook. A repository whose workflow list could not be read is
still a subscription -- the operator refreshes it once the credential is fixed.

The binding is the one row here that points at another project's table:
``(provider_kind, provider_ref)`` is an opaque pair, so the row layer stores it
without knowing that today it names a ``ct_pat_credentials`` row."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ..core.exceptions import (
    CitrackerError,
    GithubAPIError,
    RepositoryNotFoundError,
    TokenNotFoundError,
    ValidationError,
    map_api_error,
)
from ..models import AsyncRepository, AsyncWorkflow
from ..models.binding import AsyncRepositoryBinding
from ..services.github.protocols import RepositoryReader, RunReader, WebhookManager
from ..views import BindingSummary, RunRecord, _webhook_defaults
from .run import RunController

if TYPE_CHECKING:
    from ..models import AsyncWorkflowRun
    from .query import QueryController
    from .run import RunData

__all__ = [
    "DEFAULT_WEBHOOK_EVENTS",
    "RepositoryController",
    "RepositoryData",
]


logger = logging.getLogger(__name__)

#: Events a webhook subscribes to by default.
DEFAULT_WEBHOOK_EVENTS: tuple[str, ...] = ("workflow_run", "workflow_job")

#: Provider kind this service binds repositories to. A second provider gets its
#: own service rather than a branch here.
CREDENTIAL_KIND = "pat"


class RepositoryController:
    """Manage repository subscriptions and their cached workflows.

    Args:
        repositories: Repository metadata, workflows and pull requests.
        webhooks: The hooks this service owns.
        readers: Run reads, used by the pull-request lookup.
        runs: The read path that adopts or updates a run.
        data: The controller that owns repository rows.
        runs_data: The controller that owns run rows.
        query: Used to shape the runs a pull-request lookup returns.
        credential: The ambient credential, when the process has one. It wins
            over a caller-supplied id, because in a single-PAT deployment every
            repository is bound to it.
        kind: Provider kind of the ambient credential.
    """

    def __init__(
        self,
        *,
        repositories: RepositoryReader,
        webhooks: WebhookManager,
        readers: RunReader,
        runs: RunController,
        data: RepositoryData,
        runs_data: RunData,
        query: QueryController,
        credential: Any | None = None,
        kind: str = CREDENTIAL_KIND,
    ) -> None:
        self._repositories = repositories
        self._webhooks = webhooks
        self._readers = readers
        self._runs = runs
        self._store = data
        self._runs_data = runs_data
        self._query = query
        self._credential = credential
        self._kind = kind

    # -- subscriptions ---------------------------------------------------------

    async def subscribe_repository(
        self,
        full_name: str,
        token_id: int | None = None,
        create_webhook: bool = False,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
    ) -> AsyncRepository:
        """Subscribe to a repository.

        Args:
            full_name: ``owner/repo``.
            token_id: Provider-owned credential id to bind; omit to use the
                active one (or the ambient credential, when the process has one).
            create_webhook: Create the GitHub webhook too.
            webhook_url: Callback URL; defaults to ``CITRACKER_WEBHOOK_URL``.
            webhook_secret: HMAC secret; defaults to ``CITRACKER_WEBHOOK_SECRET``.

        Returns:
            The stored repository. An existing subscription is returned
            unchanged, binding included -- subscribing twice is not an error.

        Raises:
            ValidationError: Malformed name, webhook creation on an anonymous
                subscription, or a credential with no provider pair to bind.
            RepositoryNotFoundError: GitHub does not expose the repository.
        """
        name = (full_name or "").strip()
        if not name or "/" not in name:
            raise ValidationError(f"Invalid repository full name '{full_name}'")

        credential = await self._credential_for(token_id)
        if create_webhook and credential is None:
            raise ValidationError(
                "Webhook creation requires a bound credential; anonymous "
                "subscriptions must configure their webhook on GitHub's web UI."
            )

        existing = await self._store.by_name(name)
        if existing is not None:
            return existing

        info = await self._repo_info(credential, name)
        repo = await self._store.store(info, name)

        if credential is not None:
            await self._bind(repo, credential, token_id)
            logger.info(
                "Subscribed to '%s' with credential '%s'",
                name,
                getattr(credential, "identity", "") or "unknown",
            )
        else:
            logger.info("Subscribed to '%s' anonymously", name)

        defaults = _webhook_defaults()
        url = webhook_url or defaults["url"]
        secret = webhook_secret or defaults["secret"]
        await self._refresh_workflows(repo, strict=False)
        if create_webhook and url and secret:
            await self._create_hook(repo.full_name, url, secret)
        return repo

    async def unsubscribe_repository(
        self,
        full_name: str | None = None,
        repo_id: int | None = None,
    ) -> None:
        """Unsubscribe from a repository and drop its cached data.

        Remote hooks pointing at this service are removed first, on a best-effort
        basis; the local teardown is the model's, because it must complete even
        when GitHub cannot be reached.

        Raises:
            ValidationError: Neither identifier was given.
            RepositoryNotFoundError: The repository is not tracked.
        """
        if full_name is None and repo_id is None:
            raise ValidationError("Either full_name or repo_id is required")

        repo = (
            await self._store.by_name(full_name)
            if repo_id is None
            else await self._store.by_id(repo_id)
        )
        if repo is None:
            raise RepositoryNotFoundError(f"Repository '{full_name or repo_id}' is not tracked")

        await self._delete_own_hooks(repo)
        await self._store.remove(repo)

    async def bind_credential(self, full_name: str, kind: str, ref: int) -> None:
        """Point a subscribed repository at a credential, replacing any previous one.

        ``(kind, ref)`` is the provider's opaque pair -- the same one
        ``subscribe_repository`` records -- so binding afterwards lands in the
        same row as binding at subscription time. Runs and jobs are keyed by the
        repository, so changing the credential keeps the history.

        Raises:
            RepositoryNotFoundError: The repository is not tracked.
            ValidationError: No provider pair was given, so there is nothing a
                binding row could point at.
        """
        if not kind or ref is None:
            raise ValidationError(
                "A binding needs a provider kind and ref; pass a resolved credential "
                "reference such as 'pat/work'."
            )
        repo = await self.repository(full_name)
        await self._store.bind(repo.id or 0, kind, ref)

    async def unbind_credential(self, full_name: str) -> bool:
        """Return a repository to anonymous access.

        Returns:
            Whether the repository had a binding to drop. A repository that was
            already anonymous is not an error -- the caller wanted it unbound,
            and it is.

        Raises:
            RepositoryNotFoundError: The repository is not tracked.
        """
        repo = await self.repository(full_name)
        return await self._store.unbind(repo.id or 0)

    async def list_repositories(self, filter: str | None = None) -> list[AsyncRepository]:
        """List subscribed repositories, optionally filtered by name."""
        repos = await self._store.list_all()
        if filter:
            needle = filter.lower()
            repos = [repo for repo in repos if needle in repo.full_name.lower()]
        return repos

    async def discover_repositories(self, token_id: int | None = None) -> list[dict[str, Any]]:
        """List the repositories a credential can access on GitHub.

        Raises:
            TokenNotFoundError: No credential is registered to ask with.
        """
        credential = await self._credential_for(token_id)
        if credential is None:
            raise TokenNotFoundError("No credential configured")
        return list(await self._repositories.list_accessible_repos(credential))

    # -- workflows -------------------------------------------------------------

    async def list_workflows(self, repository: str) -> list[AsyncWorkflow]:
        """List the workflow definitions of a repository.

        The cache answers first; an empty cache is refreshed from GitHub once,
        because a caller asking for the workflows of a repository that was
        subscribed a moment ago must not be told there are none.
        """
        repo = await self.repository(repository)
        workflows = await self._store.workflows(repo)
        if not workflows:
            workflows = await self.refresh_workflows(repo_id=repo.id or 0)
        return workflows

    async def workflows_of(self, full_names: Sequence[str]) -> dict[str, list[AsyncWorkflow]]:
        """Each repository's workflows, keyed by ``owner/repo``.

        One query for the whole batch; a repository whose cache came back empty
        is asked of GitHub once -- the same rule :meth:`list_workflows` applies,
        because an empty cache means "not synced yet", not "has no workflows".
        A repository GitHub cannot answer for is reported with no workflows
        instead of failing the batch: this is a status report, and fourteen
        answers plus one gap beat no answer at all.

        Args:
            full_names: ``owner/repo`` names. An unknown name is skipped.
        """
        repos = []
        for name in full_names:
            repo = await self._store.by_name(name)
            if repo is not None:
                repos.append(repo)

        by_id = await self._store.workflows_of([repo.id or 0 for repo in repos])
        result: dict[str, list[AsyncWorkflow]] = {}
        for repo in repos:
            workflows = by_id.get(repo.id or 0, [])
            if not workflows:
                try:
                    workflows = await self.refresh_workflows(repo_id=repo.id or 0)
                except CitrackerError as exc:
                    logger.warning("Could not read the workflows of %s: %s", repo.full_name, exc)
                    workflows = []
            result[repo.full_name] = workflows
        return result

    async def binding_summary(self) -> BindingSummary:
        """How many subscriptions answer with a bound credential.

        A repository with no binding reads anonymously, which a public
        repository supports, so the anonymous half is a figure to report rather
        than a fault.
        """
        repos = await self._store.list_all()
        bound = await self._store.bound_ids()
        with_credential = sum(1 for repo in repos if repo.id in bound)
        return BindingSummary(bound=with_credential, anonymous=len(repos) - with_credential)

    async def get_workflow(self, repository: str, workflow: str) -> AsyncWorkflow:
        """Resolve one workflow of a repository by path, name or id.

        Raises:
            WorkflowNotFoundError: Nothing matches; the message lists what the
                repository does have.
        """
        repo = await self.repository(repository)
        await self.list_workflows(repository)
        return await self._store.workflow(repo, workflow)

    async def refresh_workflows(
        self,
        repository: str | None = None,
        repo_id: int | None = None,
    ) -> list[AsyncWorkflow]:
        """Refresh the cached workflow list of a repository from GitHub.

        Existing rows are updated in place and new ones are stored; workflows
        that disappeared upstream are kept, so historical runs stay resolvable.

        Raises:
            ValidationError: Neither identifier was given.
            RepositoryNotFoundError: The repository is not tracked.
        """
        if repo_id is None:
            if repository is None:
                raise ValidationError("Either repository or repo_id is required")
            repo = await self.repository(repository)
        else:
            repo = await self._store.by_id(repo_id)
            if repo is None:
                raise RepositoryNotFoundError(f"Repository {repo_id} not found")
        return await self._refresh_workflows(repo)

    async def get_repository_info(self, repository: str) -> dict[str, Any]:
        """Describe a subscribed repository, from the stored row."""
        repo = await self.repository(repository)
        return {
            "full_name": repo.full_name,
            "owner": repo.owner,
            "name": repo.name,
            "is_private": repo.is_private,
            "default_branch": repo.default_branch,
            "subscribed_at": repo.created_at,
        }

    # -- pull requests ---------------------------------------------------------

    async def pull_request_runs(
        self,
        repository: str,
        number: int,
        sync_jobs: bool = True,
    ) -> list[RunRecord]:
        """Find and store the CI runs of a pull request.

        GitHub has no PR-number filter on the runs endpoint, so the lookup is
        two steps: read the pull request for its ``head.sha``, fetch the runs of
        that commit, then keep the ones attributed to this pull request.

        An empty answer is a real answer (no runs, or none visible to the
        credential), not an error.

        Args:
            repository: ``owner/repo``.
            number: Pull request number.
            sync_jobs: Also store each run's jobs and steps.

        Raises:
            RepositoryNotFoundError: The repository is not tracked, or the pull
                request does not exist / is not visible.
        """
        repo = await self.repository(repository)

        try:
            pull = await self._repositories.get_pull_request(repository, number)
        except CitrackerError as exc:
            mapped = map_api_error(exc)
            if isinstance(mapped, GithubAPIError) and mapped.status_code == 404:
                raise RepositoryNotFoundError(
                    f"Pull request #{number} of '{repository}' was not found"
                ) from exc
            raise mapped from exc

        head = pull.get("head") or {}
        head_sha = str(head.get("sha") or "").strip() if isinstance(head, Mapping) else ""
        if not head_sha:
            return []

        try:
            remote_runs = await self._readers.list_runs(repository, head_sha=head_sha, per_page=100)
        except CitrackerError as exc:
            mapped = map_api_error(exc)
            if isinstance(mapped, GithubAPIError) and mapped.status_code == 404:
                return []
            raise mapped from exc

        stored: list[AsyncWorkflowRun] = []
        for remote in remote_runs:
            if not self._runs_data.belongs_to_pull_request(remote, number, head_sha):
                continue
            github_run_id = int(remote.get("id") or 0)
            if not github_run_id:
                continue

            run = await self._runs_data.run(github_run_id)
            if run is None:
                run = await self._runs_data.adopt(repo, remote)
            else:
                if not await self._runs_data.belongs_to(run, repo):
                    continue
                run = await self._runs_data.update(run, remote)

            # The pull request is already in hand, so its base branch costs
            # nothing -- and it is often the only place to get it: the run
            # payload stops carrying ``pull_requests`` once the PR is closed.
            pull_base = pull.get("base") if isinstance(pull, Mapping) else None
            pull_base_ref = (
                str(pull_base.get("ref") or "").strip() if isinstance(pull_base, Mapping) else ""
            )
            if pull_base_ref and not run.base_ref:
                run.base_ref = pull_base_ref
                await run.save()

            if sync_jobs:
                jobs = await self._readers.get_run_jobs(repository, github_run_id)
                await self._runs_data.sync_jobs(run, jobs)
            stored.append(run)

        return await self._query.records(stored, repository)

    # -- internals -------------------------------------------------------------

    async def repository(self, full_name: str) -> AsyncRepository:
        """The tracked repository, or ``RepositoryNotFoundError``.

        The existence check every write path repeats: binding a credential,
        reading a workflow list, or acting on a repository by name must refuse
        an unsubscribed name the same way.
        """
        repo = await self._store.by_name(full_name)
        if repo is None:
            raise RepositoryNotFoundError(f"Repository '{full_name}' is not tracked")
        return repo

    async def _credential_for(self, ref: int | None) -> Any | None:
        """The credential a call should use, or ``None`` for anonymous.

        The ambient credential wins over ``ref``: in a single-PAT deployment
        every repository is bound to it, and the caller has no id to pass because
        the id belongs to the deployment rather than to the request.
        """
        if self._credential is not None:
            return self._credential
        if ref is None:
            return None
        from ..credentials import get_provider

        return await get_provider(self._kind).resolve(ref)

    async def _bind(self, repo: AsyncRepository, credential: Any, ref: int | None) -> None:
        """Record that ``credential`` answers for ``repo``.

        A binding points at a row, so a credential that carries no provider pair
        cannot be bound -- that is a deployment mistake, not a caller error.
        """
        kind = getattr(credential, "kind", "") or self._kind
        provider_ref = getattr(credential, "ref", None)
        if provider_ref is None:
            provider_ref = ref
        if not kind or provider_ref is None:
            raise ValidationError(
                "The credential carries no provider pair, so it cannot be bound "
                "to a repository; resolve it from a provider or pass its id."
            )
        await self._store.bind(repo.id or 0, kind, provider_ref)

    async def _repo_info(self, credential: Any | None, full_name: str) -> Mapping[str, Any]:
        """Read repository metadata, explaining a 404 in the caller's terms.

        "Not publicly readable" and "not covered by this credential" are
        different operator mistakes, and an anonymous subscription of a private
        repository is the first one.
        """
        try:
            return await self._repositories.get_repo_info(full_name, credential=credential)
        except GithubAPIError as exc:
            if exc.status_code != 404:
                raise map_api_error(exc) from exc
            if credential is None:
                raise RepositoryNotFoundError(
                    f"Repository '{full_name}' is not publicly readable; "
                    "an anonymous subscription cannot cover a private "
                    "repository. Bind a credential instead."
                ) from exc
            raise RepositoryNotFoundError(
                f"Repository '{full_name}' is not accessible with credential "
                f"'{getattr(credential, 'identity', '') or 'unknown'}' -- it may "
                "not cover this repository, or the repository does not exist."
            ) from exc
        except CitrackerError as exc:
            raise map_api_error(exc) from exc

    async def _refresh_workflows(
        self, repo: AsyncRepository, *, strict: bool = True
    ) -> list[AsyncWorkflow]:
        """Read a repository's workflows from GitHub and store them.

        Args:
            repo: The tracked repository.
            strict: Re-raise a GitHub error instead of answering with an empty
                list. Subscribing is tolerant -- the subscription exists either
                way; an explicit refresh is not, because the caller asked.
        """
        try:
            remote = await self._repositories.get_repo_workflows(repo.full_name)
        except CitrackerError as exc:
            if strict:
                raise map_api_error(exc) from exc
            logger.warning("Could not refresh workflows for '%s': %s", repo.full_name, exc)
            return []

        workflows = await self._store.store_workflows(repo, remote)
        logger.info("Refreshed %d workflow(s) for '%s'", len(workflows), repo.full_name)
        return workflows

    async def _create_hook(self, full_name: str, url: str, secret: str) -> Mapping[str, Any] | None:
        """Create the webhook, unless this callback URL is already registered."""
        for hook in await self._webhooks.list_webhooks(full_name):
            config = hook.get("config")
            if isinstance(config, Mapping) and str(config.get("url") or "") == url:
                logger.debug("Webhook for '%s' already points at %s", full_name, url)
                return None

        created = await self._webhooks.create_webhook(
            full_name,
            {
                "url": url,
                "content_type": "json",
                "secret": secret,
                "events": list(DEFAULT_WEBHOOK_EVENTS),
            },
        )
        logger.info("Created webhook for '%s'", full_name)
        return created

    async def _delete_own_hooks(self, repo: AsyncRepository) -> None:
        """Remove the hooks whose callback URL is this service's own.

        Best effort: a GitHub failure must not stop the local teardown, or the
        orphaned subscription could never be removed.
        """
        prefix = _webhook_defaults()["url"]
        if not prefix:
            logger.debug("No webhook URL configured; keeping remote hooks of '%s'", repo.full_name)
            return

        try:
            for hook in await self._webhooks.list_webhooks(repo.full_name):
                config = hook.get("config")
                url = str(config.get("url") or "") if isinstance(config, Mapping) else ""
                hook_id = int(hook.get("id") or 0)
                if url.startswith(prefix) and hook_id:
                    await self._webhooks.delete_webhook(repo.full_name, hook_id)
                    logger.info("Deleted webhook %s of '%s'", hook_id, repo.full_name)
        except CitrackerError as exc:
            logger.warning("Could not delete webhooks of '%s': %s", repo.full_name, exc)


# -- the rows the flows above are built from ------------------------------


class RepositoryData:
    """Repositories, their credential bindings, and their cached workflows."""

    async def by_name(self, full_name: str) -> AsyncRepository | None:
        """The tracked repository called ``full_name``, or ``None``."""
        return await AsyncRepository.find_by_full_name(full_name)

    async def by_github_id(self, github_repo_id: int) -> AsyncRepository | None:
        """The tracked repository with this GitHub id, or ``None``.

        A webhook payload names the repository by id and full name; the id is the
        stable one across renames.
        """
        return await AsyncRepository.find_by_github_id(github_repo_id)

    async def by_id(self, repository_id: int) -> AsyncRepository | None:
        """The tracked repository with this local id, or ``None``."""
        return await AsyncRepository.find_one(AsyncRepository.c.id == repository_id)

    async def list_all(self, *, limit: int = 200, offset: int = 0) -> list[AsyncRepository]:
        """Subscribed repositories, oldest first."""
        return await (
            AsyncRepository.query()
            .order_by((AsyncRepository.c.id, "ASC"))
            .limit(limit)
            .offset(offset)
            .all()
        )

    async def count(self) -> int:
        """How many repositories are subscribed."""
        return await AsyncRepository.query().count()

    async def bound_ids(self) -> set[int]:
        """Ids of the repositories that answer with a credential of their own.

        A repository absent from the set reads anonymously, which a public
        repository supports -- so this is a fact to report, not a fault.
        """
        rows = await (
            AsyncRepositoryBinding.query().select(AsyncRepositoryBinding.c.repository_id).all()
        )
        return {row.repository_id for row in rows}

    async def store(self, payload: Mapping[str, object], full_name: str) -> AsyncRepository:
        """Store or refresh a repository from a GitHub payload."""
        return await AsyncRepository.upsert_from_remote(payload, full_name)

    async def bind(self, repository_id: int, kind: str, ref: int) -> None:
        """Record that the credential ``(kind, ref)`` answers for this repository.

        Replaces any previous binding: one repository is answered by one
        credential, and "changed its mind" is the same write as "bound".
        """
        await AsyncRepositoryBinding.bind(repository_id, kind, ref)

    async def unbind(self, repository_id: int) -> bool:
        """Drop the repository's binding, answering whether there was one.

        The repository itself stays subscribed; from here it reads anonymously,
        which a public repository supports.
        """
        return await AsyncRepositoryBinding.unbind(repository_id)

    async def remove(self, repo: AsyncRepository) -> None:
        """Delete a subscription and everything cached under it.

        The cascade (workflows, runs, jobs, steps, logs, the binding) is the
        model's: it is all local, and it must complete even when GitHub cannot be
        reached.
        """
        await repo.unsubscribe()

    async def workflows_of(self, repository_ids: Sequence[int]) -> dict[int, list[AsyncWorkflow]]:
        """Each repository's cached workflows, in one query.

        A repository with nothing cached is absent from the mapping rather than
        present with an empty list: "never synced" and "has no workflows" are
        different answers, and only the caller can decide which one to ask
        GitHub about.
        """
        ids = [repository_id for repository_id in repository_ids if repository_id]
        if not ids:
            return {}

        rows = await AsyncWorkflow.query().where(AsyncWorkflow.c.repository_id.in_(ids)).all()
        by_repo: dict[int, list[AsyncWorkflow]] = {}
        for workflow in rows:
            by_repo.setdefault(workflow.repository_id, []).append(workflow)
        for workflows in by_repo.values():
            workflows.sort(key=lambda workflow: workflow.id or 0)
        return by_repo

    async def workflows(self, repo: AsyncRepository) -> list[AsyncWorkflow]:
        """The cached workflows of ``repo``."""
        return await AsyncWorkflow.for_repository(repo.id or 0).all()

    async def workflow(self, repo: AsyncRepository, reference: str) -> AsyncWorkflow:
        """The repository's workflow matching ``reference``.

        The matching itself is the model's: a path, a file name, a display name
        or a numeric id, in that order.

        Raises:
            WorkflowNotFoundError: Nothing matches; the message lists what the
                repository does have.
        """
        return await AsyncWorkflow.resolve(repo.id or 0, reference)

    async def store_workflow(
        self, repo: AsyncRepository, payload: Mapping[str, object]
    ) -> AsyncWorkflow:
        """Store or refresh one workflow from a GitHub payload.

        The upsert key -- ``(repository_id, workflow_id)``, with the path as a
        second chance -- is the model's; a copy of the field mapping here would
        be a copy that drifts.
        """
        return await AsyncWorkflow(repository_id=repo.id or 0).upsert_from_remote(payload)

    async def store_workflows(
        self, repo: AsyncRepository, payloads: Sequence[Mapping[str, object]]
    ) -> list[AsyncWorkflow]:
        """Store or refresh a batch of workflows, keeping the payload order."""
        return [await self.store_workflow(repo, payload) for payload in payloads]
