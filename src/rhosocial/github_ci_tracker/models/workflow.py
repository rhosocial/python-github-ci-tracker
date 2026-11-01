# src/rhosocial/github_ci_tracker/models/workflow.py
"""Workflow model, sync and async twins.

A workflow is one GitHub Actions definition (``workflow_id``, ``name``,
``path``, ``state``) cached per subscribed repository. Runs always belong to
a workflow row, so the run's pull channel can adopt a run whose workflow the
system has never seen: :meth:`Workflow.upsert_from_remote` creates the
workflow from a remote payload (a workflow list entry or a run payload, both
accepted) keyed by ``(repository_id, workflow_id)``.

The twins are behavior-identical; the sync one exists for the project's
sync/async parity rule. There are no cross-model references here -- the
methods scope themselves by ``repository_id`` -- so this module needs no
lazy imports beyond the error raised by :meth:`Workflow.resolve`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from rhosocial.activerecord.backend.errors import IntegrityError
from rhosocial.activerecord.base.field_proxy import FieldProxy
from rhosocial.activerecord.field.timestamp import DefaultTimestampMixin
from rhosocial.activerecord.model import ActiveRecord, AsyncActiveRecord
from rhosocial.activerecord.query import ActiveQuery, AsyncActiveQuery
from rhosocial.activerecord.relation.async_descriptors import AsyncBelongsTo
from rhosocial.activerecord.relation.descriptors import BelongsTo

from .relations import AsyncOrderedHasMany, OrderedHasMany

if TYPE_CHECKING:
    from .repository import AsyncRepository, Repository
    from .run import AsyncWorkflowRun, WorkflowRun

logger = logging.getLogger(__name__)


def _as_int(value: Any) -> int | None:
    """Coerce a GitHub scalar to ``int``, returning ``None`` when impossible."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_str(value: Any, default: str = "") -> str:
    """Return a trimmed string, falling back to ``default`` for blank values."""
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _match(candidates: Sequence[Any], reference: str) -> Any | None:
    """Match a workflow reference against one repository's workflows.

    Three references, all of them stable across a rename: exact path
    (``.github/workflows/ci.yml``); the file name, with or without the
    ``.github/workflows/`` prefix; the numeric GitHub workflow id.

    A display name is **not** one of them, deliberately. ``name:`` lives in the
    workflow file, so a repository can give one workflow a different name on
    every branch: ``vistart/Dockerfiles`` calls ``build_and_deploy.yml`` "php -
    Build and Deploy", "vlmcsd - Build and Deploy" and "ubuntu - Build and
    Deploy" at the same time. The name stored in the row follows the last run
    that was written, so a query filtered by one resolves today and not
    tomorrow. ``gh`` filters runs by branch and by workflow id or file name for
    the same reason; a display name is something a run reports, not an identity.

    Args:
        candidates: The repository's stored workflows.
        reference: What the caller typed.

    Returns:
        The matching workflow, or ``None``.
    """
    wanted = _clean_str(reference)
    if not wanted:
        return None

    paths = [wanted]
    if not wanted.startswith(".github/"):
        paths.append(f".github/workflows/{wanted}")
    for path in paths:
        for workflow in candidates:
            if workflow.path == path:
                return workflow

    suffix = f"/{wanted}"
    for workflow in candidates:
        if workflow.path.endswith(suffix):
            return workflow

    numeric_id = _as_int(wanted)
    if numeric_id is not None:
        for workflow in candidates:
            if workflow.workflow_id == numeric_id:
                return workflow

    return None


def _not_found(reference: str, candidates: Sequence[Any]) -> Any:
    """The error for an unmatched reference, listing what the repository has.

    The list is the only place a caller can learn the exact spelling GitHub uses,
    so it travels with the error instead of behind another call. Paths lead each
    entry because they are the stable reference: a display name can differ from
    branch to branch, and the stored one follows the last run written.
    """
    from .errors import WorkflowNotFoundError

    available = ", ".join(
        sorted(f"{workflow.path} (id {workflow.workflow_id or '?'})" for workflow in candidates)
    )
    return WorkflowNotFoundError(
        f"Workflow '{reference}' not found. Use a path, a file name or the numeric "
        f"workflow id: "
        + (
            available
            or "none are stored yet; call list_repos for this repository to refresh "
            "them from GitHub"
        )
    )


class Workflow(DefaultTimestampMixin, ActiveRecord):
    """GitHub Actions workflow definition, synchronous twin."""

    __table_name__ = "ct_workflows"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    repository_id: int = 0
    #: Numeric GitHub workflow id. ``0`` for rows adopted before the id was
    #: learned from a run payload; :meth:`upsert_from_remote` fills it in.
    workflow_id: int = 0
    name: str = ""
    path: str = ""
    state: str = "active"

    #: Owning repository (pairs with :attr:`Repository.workflows`).
    repository: ClassVar[BelongsTo[Repository]] = BelongsTo(
        foreign_key="repository_id", inverse_of="workflows"
    )
    #: Runs of this workflow, newest first.
    runs: ClassVar[OrderedHasMany[WorkflowRun]] = OrderedHasMany(
        foreign_key="workflow_id",
        inverse_of="workflow",
        order_key=lambda run: run.run_number,
        reverse=True,
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    def for_repository(cls, repository_id: int) -> ActiveQuery:
        """Query builder over the cached workflows of one repository."""
        return cls.query().where(cls.c.repository_id == repository_id)

    @classmethod
    def resolve(cls, repository_id: int, ref: str) -> Workflow:
        """Resolve a workflow reference to the stored record.

        The reference may be an exact path, a file name, a display name or a
        numeric GitHub workflow id; see :func:`_match` for the order and the
        case rules.

        Args:
            repository_id: Local repository id the workflow belongs to.
            ref: Path, file name, workflow name or numeric id.

        Returns:
            The stored :class:`Workflow` record.

        Raises:
            WorkflowNotFoundError: If no level matches; the message lists the
                workflows the repository does have.
        """
        candidates = cls.query().where(cls.c.repository_id == repository_id).all()
        workflow = _match(candidates, ref)
        if workflow is None:
            raise _not_found(ref, candidates)
        return workflow

    # ---- behavior -------------------------------------------------------------

    def upsert_from_remote(self, remote: Mapping[str, Any]) -> Workflow:
        """Create or update this repository's workflow from a remote payload.

        The GitHub workflow id is read from ``workflow_id`` (run payloads),
        falling back to ``id`` (workflow list payloads) and then to the
        receiving instance. The lookup is keyed by
        ``(repository_id, workflow_id)``:

        1. a row with that workflow id is refreshed in place (name, path,
           state) and returned;
        2. otherwise a row with the same ``path`` adopts the payload, and a
           missing workflow id is filled in;
        3. otherwise the payload is stored as a new row -- the receiving
           instance itself when it is not persisted yet, a fresh row when it
           is.

        Args:
            remote: A GitHub workflow object or a run payload; both carry
                ``name`` and ``path``, and between them identify the
                workflow through ``workflow_id`` (run) or ``id`` (workflow).

        Returns:
            The persisted :class:`Workflow` record.
        """
        cls = type(self)

        github_workflow_id = (
            _as_int(remote.get("workflow_id")) or _as_int(remote.get("id")) or self.workflow_id or 0
        )
        name = _clean_str(remote.get("name"), self.name)
        path = _clean_str(remote.get("path"), self.path)
        state = _clean_str(remote.get("state"), self.state or "active")

        if github_workflow_id:
            existing = cls.find_one(
                (cls.c.repository_id == self.repository_id)
                & (cls.c.workflow_id == github_workflow_id)
            )
            if existing is not None:
                existing._refresh_fields(name=name, path=path, state=state)
                return existing

        if path:
            existing = cls.find_one(
                (cls.c.repository_id == self.repository_id) & (cls.c.path == path)
            )
            if existing is not None:
                if github_workflow_id and not existing.workflow_id:
                    existing.workflow_id = github_workflow_id
                existing._refresh_fields(name=name, path=path, state=state)
                return existing

        record = self if self.id is None else cls(repository_id=self.repository_id)
        record.workflow_id = github_workflow_id
        record.name = name
        record.path = path
        record.state = state
        try:
            record.save()
        except IntegrityError:
            # Another run of the same workflow was being adopted at the same
            # moment; the unique key is (repository_id, workflow_id).
            existing = cls.find_one(
                (cls.c.repository_id == self.repository_id)
                & (cls.c.workflow_id == github_workflow_id)
            )
            if existing is None:
                raise
            return existing
        logger.debug(
            "Stored workflow %s ('%s') for repository %s",
            github_workflow_id,
            path,
            self.repository_id,
        )
        return record

    def _refresh_fields(self, name: str, path: str, state: str) -> None:
        """Copy non-blank remote fields onto this row, saving when changed."""
        if name and name != self.name:
            self.name = name
        if path and path != self.path:
            self.path = path
        if state and state != self.state:
            self.state = state
        if self.is_dirty:
            self.save()


class AsyncWorkflow(DefaultTimestampMixin, AsyncActiveRecord):
    """GitHub Actions workflow definition, asynchronous twin.

    Behavior-identical to :class:`Workflow`; every method performs the same
    checks and raises the same exceptions.
    """

    __table_name__ = "ct_workflows"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    repository_id: int = 0
    #: Numeric GitHub workflow id. ``0`` for rows adopted before the id was
    #: learned from a run payload; :meth:`upsert_from_remote` fills it in.
    workflow_id: int = 0
    name: str = ""
    path: str = ""
    state: str = "active"

    #: Owning repository (pairs with :attr:`AsyncRepository.workflows`).
    repository: ClassVar[AsyncBelongsTo[AsyncRepository]] = AsyncBelongsTo(
        foreign_key="repository_id", inverse_of="workflows"
    )
    #: Runs of this workflow, newest first.
    runs: ClassVar[AsyncOrderedHasMany[AsyncWorkflowRun]] = AsyncOrderedHasMany(
        foreign_key="workflow_id",
        inverse_of="workflow",
        order_key=lambda run: run.run_number,
        reverse=True,
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    def for_repository(cls, repository_id: int) -> AsyncActiveQuery:
        """Query builder over the cached workflows of one repository."""
        return cls.query().where(cls.c.repository_id == repository_id)

    @classmethod
    async def resolve(cls, repository_id: int, ref: str) -> AsyncWorkflow:
        """Resolve a workflow reference to the stored record.

        See :meth:`Workflow.resolve` for the matching order (exact path, file
        name, display name, numeric id) and the exception.
        """
        candidates = await cls.query().where(cls.c.repository_id == repository_id).all()
        workflow = _match(candidates, ref)
        if workflow is None:
            raise _not_found(ref, candidates)
        return workflow

    # ---- behavior -------------------------------------------------------------

    async def upsert_from_remote(self, remote: Mapping[str, Any]) -> AsyncWorkflow:
        """Create or update this repository's workflow from a remote payload.

        See :meth:`Workflow.upsert_from_remote` for the key resolution and
        the three-step match; the stored field semantics are identical.
        """
        cls = type(self)

        github_workflow_id = (
            _as_int(remote.get("workflow_id")) or _as_int(remote.get("id")) or self.workflow_id or 0
        )
        name = _clean_str(remote.get("name"), self.name)
        path = _clean_str(remote.get("path"), self.path)
        state = _clean_str(remote.get("state"), self.state or "active")

        if github_workflow_id:
            existing = await cls.find_one(
                (cls.c.repository_id == self.repository_id)
                & (cls.c.workflow_id == github_workflow_id)
            )
            if existing is not None:
                await existing._refresh_fields(name=name, path=path, state=state)
                return existing

        if path:
            existing = await cls.find_one(
                (cls.c.repository_id == self.repository_id) & (cls.c.path == path)
            )
            if existing is not None:
                if github_workflow_id and not existing.workflow_id:
                    existing.workflow_id = github_workflow_id
                await existing._refresh_fields(name=name, path=path, state=state)
                return existing

        record = self if self.id is None else cls(repository_id=self.repository_id)
        record.workflow_id = github_workflow_id
        record.name = name
        record.path = path
        record.state = state
        try:
            await record.save()
        except IntegrityError:
            # See :meth:`Workflow.upsert_from_remote`.
            existing = await cls.find_one(
                (cls.c.repository_id == self.repository_id)
                & (cls.c.workflow_id == github_workflow_id)
            )
            if existing is None:
                raise
            return existing
        logger.debug(
            "Stored workflow %s ('%s') for repository %s",
            github_workflow_id,
            path,
            self.repository_id,
        )
        return record

    async def _refresh_fields(self, name: str, path: str, state: str) -> None:
        """Copy non-blank remote fields onto this row, saving when changed."""
        if name and name != self.name:
            self.name = name
        if path and path != self.path:
            self.path = path
        if state and state != self.state:
            self.state = state
        if self.is_dirty:
            await self.save()
