# src/rhosocial/github_ci_tracker/models/binding.py
"""Repository → credential binding, sync and async twins.

A binding is the single routing rule of the service: given a repository, which
credential answers for it. It deliberately stores an opaque pair
(``provider_kind`` + ``provider_ref``) rather than a foreign key to any
credential table.

That missing foreign key is a deliberate trade. A foreign key would make the
core depend on the shape of every provider's table, which is exactly the coupling
the provider split exists to avoid -- and a polymorphic key referencing
"whatever table owns kind=pat" is not something PostgreSQL can enforce anyway.
Two disciplines replace it:

* a provider refuses to delete a credential while ``count_bindings`` is non-zero;
* a provider that does delete one calls ``release``, which drops these rows and
  returns the repository to "no credential" (anonymous reads still work for
  public repositories).

``UNIQUE(repository_id)`` guarantees one answer per repository: two providers can
never both answer for the same repository at once.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from rhosocial.activerecord.base.field_proxy import FieldProxy
from rhosocial.activerecord.field.timestamp import DefaultTimestampMixin
from rhosocial.activerecord.model import ActiveRecord, AsyncActiveRecord
from rhosocial.activerecord.query import ActiveQuery, AsyncActiveQuery
from rhosocial.activerecord.relation.async_descriptors import AsyncBelongsTo
from rhosocial.activerecord.relation.descriptors import BelongsTo

if TYPE_CHECKING:
    from .repository import AsyncRepository, Repository

logger = logging.getLogger(__name__)


class RepositoryBinding(DefaultTimestampMixin, ActiveRecord):
    """Which credential answers for a repository, synchronous twin."""

    __table_name__ = "ct_repository_bindings"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    #: The repository this binding covers. Unique: one credential per repository.
    repository_id: int = 0
    #: Provider registry key (``pat`` today, ``github_app`` later).
    provider_kind: str = ""
    #: Provider-owned row id. Opaque to the core by design.
    provider_ref: int = 0

    #: Owning repository (pairs with :attr:`Repository.binding`).
    repository: ClassVar[BelongsTo[Repository]] = BelongsTo(
        foreign_key="repository_id", inverse_of="binding"
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    def for_repository(cls, repository_id: int) -> RepositoryBinding | None:
        """Return the binding of one repository, or ``None`` for anonymous."""
        return cls.find_one(cls.c.repository_id == repository_id)

    @classmethod
    def for_provider(cls, kind: str, ref: int) -> ActiveQuery:
        """Query builder over the bindings of one credential."""
        return cls.query().where((cls.c.provider_kind == kind) & (cls.c.provider_ref == ref))

    @classmethod
    def count_for_provider(cls, kind: str, ref: int) -> int:
        """How many repositories a credential answers for."""
        return cls.for_provider(kind, ref).count()

    @classmethod
    def release_for_provider(cls, kind: str, ref: int) -> int:
        """Drop every binding of a credential; returns how many were removed."""
        removed = cls.for_provider(kind, ref).delete_all()
        if removed:
            logger.info("Released %d binding(s) of %s/%s", removed, kind, ref)
        return removed

    # ---- behavior -------------------------------------------------------------

    @classmethod
    def bind(cls, repository_id: int, kind: str, ref: int) -> RepositoryBinding:
        """Bind a repository to a credential, replacing any previous binding.

        A rebind keeps the row: the repository id is what identifies the
        subscription, and its history predates the credential change.
        """
        existing = cls.for_repository(repository_id)
        if existing is not None:
            if existing.provider_kind == kind and existing.provider_ref == ref:
                return existing
            previous = f"{existing.provider_kind}/{existing.provider_ref}"
            existing.provider_kind = kind
            existing.provider_ref = ref
            existing.save()
            logger.info(
                "Rebound repository %s from %s to %s/%s", repository_id, previous, kind, ref
            )
            return existing

        record = cls(repository_id=repository_id, provider_kind=kind, provider_ref=ref)
        record.save()
        return record

    @classmethod
    def unbind(cls, repository_id: int) -> bool:
        """Drop a repository's binding; ``True`` when one existed."""
        existing = cls.for_repository(repository_id)
        if existing is None:
            return False
        existing.delete()
        return True

    @property
    def ref(self) -> tuple[str, int]:
        """The opaque pair the provider registry is asked to resolve."""
        return (self.provider_kind, self.provider_ref)

    def describe(self) -> dict[str, Any]:
        """Serialize for protocol responses."""
        return {
            "provider_kind": self.provider_kind,
            "provider_ref": self.provider_ref,
            "credential": f"{self.provider_kind}/{self.provider_ref}",
        }


class AsyncRepositoryBinding(DefaultTimestampMixin, AsyncActiveRecord):
    """Which credential answers for a repository, asynchronous twin.

    Behavior-identical to :class:`RepositoryBinding`; every method performs the
    same queries and raises the same exceptions.
    """

    __table_name__ = "ct_repository_bindings"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    repository_id: int = 0
    provider_kind: str = ""
    provider_ref: int = 0

    repository: ClassVar[AsyncBelongsTo[AsyncRepository]] = AsyncBelongsTo(
        foreign_key="repository_id", inverse_of="binding"
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    async def for_repository(cls, repository_id: int) -> AsyncRepositoryBinding | None:
        """Return the binding of one repository, or ``None`` for anonymous."""
        return await cls.find_one(cls.c.repository_id == repository_id)

    @classmethod
    def for_provider(cls, kind: str, ref: int) -> AsyncActiveQuery:
        """Query builder over the bindings of one credential."""
        return cls.query().where((cls.c.provider_kind == kind) & (cls.c.provider_ref == ref))

    @classmethod
    async def count_for_provider(cls, kind: str, ref: int) -> int:
        """How many repositories a credential answers for."""
        return await cls.for_provider(kind, ref).count()

    @classmethod
    async def release_for_provider(cls, kind: str, ref: int) -> int:
        """Drop every binding of a credential; returns how many were removed."""
        removed = await cls.for_provider(kind, ref).delete_all()
        if removed:
            logger.info("Released %d binding(s) of %s/%s", removed, kind, ref)
        return removed

    # ---- behavior -------------------------------------------------------------

    @classmethod
    async def bind(cls, repository_id: int, kind: str, ref: int) -> AsyncRepositoryBinding:
        """Bind a repository to a credential, replacing any previous binding."""
        existing = await cls.for_repository(repository_id)
        if existing is not None:
            if existing.provider_kind == kind and existing.provider_ref == ref:
                return existing
            previous = f"{existing.provider_kind}/{existing.provider_ref}"
            existing.provider_kind = kind
            existing.provider_ref = ref
            await existing.save()
            logger.info(
                "Rebound repository %s from %s to %s/%s", repository_id, previous, kind, ref
            )
            return existing

        record = cls(repository_id=repository_id, provider_kind=kind, provider_ref=ref)
        await record.save()
        return record

    @classmethod
    async def unbind(cls, repository_id: int) -> bool:
        """Drop a repository's binding; ``True`` when one existed."""
        existing = await cls.for_repository(repository_id)
        if existing is None:
            return False
        await existing.delete()
        return True

    @property
    def ref(self) -> tuple[str, int]:
        """The opaque pair the provider registry is asked to resolve."""
        return (self.provider_kind, self.provider_ref)

    def describe(self) -> dict[str, Any]:
        """Serialize for protocol responses."""
        return {
            "provider_kind": self.provider_kind,
            "provider_ref": self.provider_ref,
            "credential": f"{self.provider_kind}/{self.provider_ref}",
        }
