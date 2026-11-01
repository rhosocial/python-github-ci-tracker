# src/rhosocial/github_ci_tracker/models/repository.py
"""Repository subscription model, sync and async twins.

A subscription is the anchor of every CI operation: it registers
``owner/repo``, caches the repository metadata (id, default branch) plus its
workflow list, and -- through :mod:`models.binding` -- records which credential
answers for it. A repository with no binding is an anonymous subscription of a
publicly readable repository.

The credential itself lives in a provider (``credentials/pat`` today). This
module never imports a provider's models: it reads the binding's opaque
``(kind, ref)`` pair and asks the registry to resolve it.

The model stores; it does not fetch. Subscribing, unsubscribing and refreshing
the cached workflows all involve a GitHub call, so they live in the controllers
(``core/services.py``); what remains here is the local half of each operation --
:meth:`~Repository.upsert_from_remote` to store a payload the controller
fetched, and :meth:`~Repository.unsubscribe` to drop the local rows the
controller has already dealt with remotely. The twins are behavior-identical;
the sync one exists for the project's sync/async parity rule.

References that would make the import graph cyclic -- the token, workflow,
run, job, step and log models, plus the ``core`` package (whose ``__init__``
imports the event handlers, which import the models) -- are imported lazily
inside method bodies; the type annotations use ``TYPE_CHECKING`` imports
instead.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar

from rhosocial.activerecord.base.field_proxy import FieldProxy
from rhosocial.activerecord.field.timestamp import DefaultTimestampMixin
from rhosocial.activerecord.model import ActiveRecord, AsyncActiveRecord
from rhosocial.activerecord.relation.async_descriptors import AsyncHasOne
from rhosocial.activerecord.relation.descriptors import HasOne

from .relations import AsyncOrderedHasMany, OrderedHasMany

if TYPE_CHECKING:
    from .binding import AsyncRepositoryBinding, RepositoryBinding
    from .run import AsyncWorkflowRun, WorkflowRun
    from .workflow import AsyncWorkflow, Workflow

logger = logging.getLogger(__name__)

#: Default pagination for the local run pages.
DEFAULT_PER_PAGE = 30
#: Upper bound of one page, mirroring the GitHub API limit.
MAX_PER_PAGE = 100


def _clean_str(value: Any, default: str = "") -> str:
    """Return a trimmed string, falling back to ``default`` for blank values."""
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _as_int(value: Any) -> int | None:
    """Coerce a GitHub scalar to ``int``, returning ``None`` when impossible."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _repo_owner(repo_info: Mapping[str, Any], full_name: str) -> str:
    """Resolve the owner login from a repository payload."""
    owner = repo_info.get("owner")
    if isinstance(owner, Mapping):
        return _clean_str(owner.get("login")) or full_name.split("/", 1)[0]
    return _clean_str(owner) or full_name.split("/", 1)[0]


def _repo_name(repo_info: Mapping[str, Any], full_name: str) -> str:
    """Resolve the repository name from a repository payload."""
    name = _clean_str(repo_info.get("name"))
    if name:
        return name
    if "/" in full_name:
        return full_name.split("/", 1)[1]
    return full_name


class Repository(DefaultTimestampMixin, ActiveRecord):
    """GitHub repository subscription, synchronous twin."""

    __table_name__ = "ct_repositories"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    github_repo_id: int = 0
    full_name: str = ""
    owner: str = ""
    name: str = ""
    is_private: bool = False
    default_branch: str = "main"

    #: Cached workflows of this repository, in local id order.
    workflows: ClassVar[OrderedHasMany[Workflow]] = OrderedHasMany(
        foreign_key="repository_id",
        inverse_of="repository",
        order_key=lambda workflow: workflow.id or 0,
    )
    #: Which credential answers for this repository; absent for anonymous.
    binding: ClassVar[HasOne[RepositoryBinding]] = HasOne(
        foreign_key="repository_id", inverse_of="repository"
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    def find_by_full_name(cls, full_name: str) -> Repository | None:
        """Return the subscription for ``owner/repo``, or ``None``."""
        return cls.find_one(cls.c.full_name == full_name)

    @classmethod
    def find_by_github_id(cls, github_repo_id: int) -> Repository | None:
        """Return the subscription for a GitHub repository id, or ``None``."""
        return cls.find_one(cls.c.github_repo_id == github_repo_id)

    # ---- behavior -------------------------------------------------------------

    @classmethod
    def upsert_from_remote(
        cls,
        repo_info: Mapping[str, Any],
        full_name: str | None = None,
    ) -> Repository:
        """Store a repository from GitHub metadata, or return the existing row.

        Idempotent by ``full_name``: subscribing twice must return the row the
        first call stored, because the operator's declaration wins over
        whatever GitHub reports later, and a silent rebind is exactly what the
        ``UNIQUE(repository_id)`` binding is there to prevent.

        Args:
            repo_info: A GitHub repository object.
            full_name: ``owner/repo`` as the caller asked for it. The payload's
                own ``full_name`` is used when omitted, and the caller's
                spelling wins when both are present: it is the name the caller
                confirmed, and GitHub answers a slightly different one for a
                repository that was renamed mid-flight.

        Returns:
            The stored :class:`Repository` record.
        """
        name = _clean_str(full_name or repo_info.get("full_name"))
        existing = cls.find_one(cls.c.full_name == name)
        if existing is not None:
            return existing

        repo = cls(
            github_repo_id=_as_int(repo_info.get("id")) or 0,
            full_name=name,
            owner=_repo_owner(repo_info, name),
            name=_repo_name(repo_info, name),
            is_private=bool(repo_info.get("is_private", False)),
            default_branch=_clean_str(repo_info.get("default_branch"), "main"),
        )
        repo.save()
        return repo

    def unsubscribe(self) -> None:
        """Drop this repository's cached data, its binding and the row.

        Local only: removing the webhooks this service created on GitHub is the
        controller's job, so the model never needs a client. The DDL declares
        plain foreign keys, so the dependent rows go first.
        """
        self._purge_repository_data()
        # The binding goes with the subscription: leaving it behind would keep
        # the credential "in use" forever.
        if self.id is not None:
            from .binding import RepositoryBinding

            RepositoryBinding.unbind(self.id)
        self.delete()
        logger.info("Unsubscribed from '%s'", self.full_name)

    def _purge_repository_data(self) -> None:
        """Delete the cached CI data of this repository.

        The deletes are set-based (``select(id)`` then ``delete_all``): loading
        the cascade into memory would materialize the whole CI history just to
        drop it. They are also explicit rather than left to the schema's
        ``ON DELETE CASCADE``, because the test harness binds every model to
        its own database, where a cross-table cascade cannot reach.
        """
        from .attempt import RunAttempt
        from .job import Job
        from .log import JobLog
        from .run import WorkflowRun
        from .step import Step
        from .workflow import Workflow

        workflow_ids = [
            row.id
            for row in Workflow.query()
            .select(Workflow.c.id)
            .where(Workflow.c.repository_id == self.id or 0)
            .all()
            if row.id is not None
        ]
        if not workflow_ids:
            return

        run_ids = [
            row.id
            for row in WorkflowRun.query()
            .select(WorkflowRun.c.id)
            .where(WorkflowRun.c.workflow_id.in_(workflow_ids))
            .all()
            if row.id is not None
        ]
        job_ids: list[int] = []
        if run_ids:
            # A job belongs to an attempt, so the two-hop read: the runs'
            # attempts, then the jobs of those attempts.
            attempt_ids = [
                row.id
                for row in RunAttempt.query()
                .select(RunAttempt.c.id)
                .where(RunAttempt.c.run_id.in_(run_ids))
                .all()
                if row.id is not None
            ]
            if attempt_ids:
                job_ids = [
                    row.id
                    for row in Job.query()
                    .select(Job.c.id)
                    .where(Job.c.attempt_id.in_(attempt_ids))
                    .all()
                    if row.id is not None
                ]
        if job_ids:
            JobLog.query().where(JobLog.c.job_id.in_(job_ids)).delete_all()
            Step.query().where(Step.c.job_id.in_(job_ids)).delete_all()
            Job.query().where(Job.c.id.in_(job_ids)).delete_all()
        if run_ids:
            WorkflowRun.query().where(WorkflowRun.c.id.in_(run_ids)).delete_all()
        Workflow.query().where(Workflow.c.id.in_(workflow_ids)).delete_all()

    # ``workflows`` is the relation descriptor declared above; call it as
    # ``repo.workflows()`` to get the cached workflows in local id order.

    def resolve_workflow(self, ref: str) -> Workflow:
        """Resolve a workflow by file name, full path or numeric GitHub id.

        Args:
            ref: File name (``ci.yml``), path (``.github/workflows/ci.yml``)
                or numeric id.

        Returns:
            The stored workflow record.

        Raises:
            WorkflowNotFoundError: If no local workflow matches.
        """
        from .errors import WorkflowNotFoundError
        from .workflow import Workflow

        candidates = [ref]
        if not ref.startswith(".github/"):
            candidates.append(f".github/workflows/{ref}")

        repo_id = self.id or 0
        for candidate in candidates:
            workflow = Workflow.find_one(
                (Workflow.c.repository_id == repo_id) & (Workflow.c.path == candidate)
            )
            if workflow is not None:
                return workflow

        numeric_id = _as_int(ref)
        if numeric_id is not None:
            workflow = Workflow.find_one(
                (Workflow.c.repository_id == repo_id) & (Workflow.c.workflow_id == numeric_id)
            )
            if workflow is not None:
                return workflow

        raise WorkflowNotFoundError(f"Workflow '{ref}' not found in repository '{self.full_name}'")

    def runs(
        self,
        workflow: str | None = None,
        branch: str | None = None,
        per_page: int = 20,
        page: int = 1,
    ) -> list[WorkflowRun]:
        """List stored runs of this repository, newest first.

        Reads the local database only; no GitHub call is made. There is no
        ``status`` filter: status is a fact about an attempt, so a run row does
        not carry one.

        Args:
            workflow: Optional workflow file name, path or numeric id.
            branch: Optional branch filter.
            per_page: Page size (clamped to ``[1, MAX_PER_PAGE]``).
            page: 1-based page number.

        Returns:
            Matching runs ordered by ``run_number`` descending.
        """
        from .run import WorkflowRun

        if workflow:
            wf = self.resolve_workflow(workflow)
            workflow_ids = [wf.id or 0]
        else:
            workflow_ids = [w.id or 0 for w in self.workflows()]
            if not workflow_ids:
                return []

        size = DEFAULT_PER_PAGE if per_page < 1 else min(per_page, MAX_PER_PAGE)
        page_number = page if page > 0 else 1

        query = WorkflowRun.query().where(WorkflowRun.c.workflow_id.in_(workflow_ids))
        if branch:
            query = query.where(WorkflowRun.c.branch == branch)

        return (
            query.order_by((WorkflowRun.c.run_number, "DESC"))
            .limit(size)
            .offset((page_number - 1) * size)
            .all()
        )


class AsyncRepository(DefaultTimestampMixin, AsyncActiveRecord):
    """GitHub repository subscription, asynchronous twin.

    Behavior-identical to :class:`Repository`; every method performs the
    same requests and raises the same exceptions.
    """

    __table_name__ = "ct_repositories"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    github_repo_id: int = 0
    full_name: str = ""
    owner: str = ""
    name: str = ""
    is_private: bool = False
    default_branch: str = "main"

    #: Cached workflows of this repository, in local id order.
    workflows: ClassVar[AsyncOrderedHasMany[AsyncWorkflow]] = AsyncOrderedHasMany(
        foreign_key="repository_id",
        inverse_of="repository",
        order_key=lambda workflow: workflow.id or 0,
    )
    #: Which credential answers for this repository; absent for anonymous.
    binding: ClassVar[AsyncHasOne[AsyncRepositoryBinding]] = AsyncHasOne(
        foreign_key="repository_id", inverse_of="repository"
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    async def find_by_full_name(cls, full_name: str) -> AsyncRepository | None:
        """Return the subscription for ``owner/repo``, or ``None``."""
        return await cls.find_one(cls.c.full_name == full_name)

    @classmethod
    async def find_by_github_id(cls, github_repo_id: int) -> AsyncRepository | None:
        """Return the subscription for a GitHub repository id, or ``None``."""
        return await cls.find_one(cls.c.github_repo_id == github_repo_id)

    # ---- behavior -------------------------------------------------------------

    @classmethod
    async def upsert_from_remote(
        cls,
        repo_info: Mapping[str, Any],
        full_name: str | None = None,
    ) -> AsyncRepository:
        """Store a repository from GitHub metadata, or return the existing row.

        See :meth:`Repository.upsert_from_remote` for the idempotence rule and
        the ``full_name`` precedence; the stored fields are identical.
        """
        name = _clean_str(full_name or repo_info.get("full_name"))
        existing = await cls.find_one(cls.c.full_name == name)
        if existing is not None:
            return existing

        repo = cls(
            github_repo_id=_as_int(repo_info.get("id")) or 0,
            full_name=name,
            owner=_repo_owner(repo_info, name),
            name=_repo_name(repo_info, name),
            is_private=bool(repo_info.get("is_private", False)),
            default_branch=_clean_str(repo_info.get("default_branch"), "main"),
        )
        await repo.save()
        return repo

    async def unsubscribe(self) -> None:
        """Drop this repository's cached data, its binding and the row.

        See :meth:`Repository.unsubscribe`. Local only: the controller removes
        the webhooks first, so the model needs no client.
        """
        from .binding import AsyncRepositoryBinding

        await self._purge_repository_data()
        # The binding goes with the subscription: leaving it behind would keep
        # the credential "in use" forever, and the next delete would be refused
        # for a repository nobody is tracking any more.
        if self.id is not None:
            await AsyncRepositoryBinding.unbind(self.id)
        await self.delete()
        logger.info("Unsubscribed from '%s'", self.full_name)

    async def _purge_repository_data(self) -> None:
        """Delete the cached CI data of this repository.

        The deletes are set-based (``select(id)`` then ``delete_all``): loading
        the cascade into memory would materialize the whole CI history just to
        drop it. They are also explicit rather than left to the schema's
        ``ON DELETE CASCADE``, because the test harness binds every model to
        its own database, where a cross-table cascade cannot reach.
        """
        from .attempt import AsyncRunAttempt
        from .job import AsyncJob
        from .log import AsyncJobLog
        from .run import AsyncWorkflowRun
        from .step import AsyncStep
        from .workflow import AsyncWorkflow

        workflow_ids = [
            row.id
            for row in await AsyncWorkflow.query()
            .select(AsyncWorkflow.c.id)
            .where(AsyncWorkflow.c.repository_id == self.id or 0)
            .all()
            if row.id is not None
        ]
        if not workflow_ids:
            return

        run_ids = [
            row.id
            for row in await AsyncWorkflowRun.query()
            .select(AsyncWorkflowRun.c.id)
            .where(AsyncWorkflowRun.c.workflow_id.in_(workflow_ids))
            .all()
            if row.id is not None
        ]
        job_ids: list[int] = []
        if run_ids:
            attempt_ids = [
                row.id
                for row in await AsyncRunAttempt.query()
                .select(AsyncRunAttempt.c.id)
                .where(AsyncRunAttempt.c.run_id.in_(run_ids))
                .all()
                if row.id is not None
            ]
            if attempt_ids:
                job_ids = [
                    row.id
                    for row in await AsyncJob.query()
                    .select(AsyncJob.c.id)
                    .where(AsyncJob.c.attempt_id.in_(attempt_ids))
                    .all()
                    if row.id is not None
                ]
        if job_ids:
            await AsyncJobLog.query().where(AsyncJobLog.c.job_id.in_(job_ids)).delete_all()
            await AsyncStep.query().where(AsyncStep.c.job_id.in_(job_ids)).delete_all()
            await AsyncJob.query().where(AsyncJob.c.id.in_(job_ids)).delete_all()
        if run_ids:
            await AsyncWorkflowRun.query().where(AsyncWorkflowRun.c.id.in_(run_ids)).delete_all()
        await AsyncWorkflow.query().where(AsyncWorkflow.c.id.in_(workflow_ids)).delete_all()

    # ``workflows`` is the relation descriptor declared above; call it as
    # ``repo.workflows()`` to get the cached workflows in local id order.

    async def resolve_workflow(self, ref: str) -> Workflow:
        """Resolve a workflow by file name, full path or numeric GitHub id.

        See :meth:`Repository.resolve_workflow`; the resolution order and the
        exception set are identical.

        Raises:
            WorkflowNotFoundError: If no local workflow matches.
        """
        from .errors import WorkflowNotFoundError
        from .workflow import AsyncWorkflow

        candidates = [ref]
        if not ref.startswith(".github/"):
            candidates.append(f".github/workflows/{ref}")

        repo_id = self.id or 0
        for candidate in candidates:
            workflow = await AsyncWorkflow.find_one(
                (AsyncWorkflow.c.repository_id == repo_id) & (AsyncWorkflow.c.path == candidate)
            )
            if workflow is not None:
                return workflow

        numeric_id = _as_int(ref)
        if numeric_id is not None:
            workflow = await AsyncWorkflow.find_one(
                (AsyncWorkflow.c.repository_id == repo_id)
                & (AsyncWorkflow.c.workflow_id == numeric_id)
            )
            if workflow is not None:
                return workflow

        raise WorkflowNotFoundError(f"Workflow '{ref}' not found in repository '{self.full_name}'")

    async def runs(
        self,
        workflow: str | None = None,
        branch: str | None = None,
        per_page: int = 20,
        page: int = 1,
    ) -> list[AsyncWorkflowRun]:
        """List stored runs of this repository, newest first.

        See :meth:`Repository.runs`; no ``status`` filter, for the same reason.
        """
        from .run import AsyncWorkflowRun

        if workflow:
            wf = await self.resolve_workflow(workflow)
            workflow_ids = [wf.id or 0]
        else:
            workflow_ids = [w.id or 0 for w in await self.workflows()]
            if not workflow_ids:
                return []

        size = DEFAULT_PER_PAGE if per_page < 1 else min(per_page, MAX_PER_PAGE)
        page_number = page if page > 0 else 1

        query = AsyncWorkflowRun.query().where(AsyncWorkflowRun.c.workflow_id.in_(workflow_ids))
        if branch:
            query = query.where(AsyncWorkflowRun.c.branch == branch)

        return (
            await query.order_by((AsyncWorkflowRun.c.run_number, "DESC"))
            .limit(size)
            .offset((page_number - 1) * size)
            .all()
        )
