# src/rhosocial/github_ci_tracker/models/relations.py
"""Relation descriptors that keep a stable order.

The ORM's default ``HasMany`` loader fetches a collection without an
``ORDER BY``, so ``run.jobs()`` / ``job.steps()`` / ``repo.workflows()`` would
come back in whatever order the backend happened to produce. Those models
promise a stable order (job id, step number, workflow id), so they declare
:class:`OrderedHasMany` / :class:`AsyncOrderedHasMany` instead: the ordering
belongs to the relation, not to every call site.

Ordering happens after the batched fetch, which keeps the single
``WHERE ... IN`` query that avoids N+1 -- one query per relation however many
parents are loaded. The descriptor caches the result, so a second access on the
same instance issues no query at all.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Generic, TypeVar

from rhosocial.activerecord.relation.async_descriptors import (
    AsyncDefaultRelationLoader,
    AsyncHasMany,
)
from rhosocial.activerecord.relation.descriptors import DefaultIRelationLoader, HasMany

T = TypeVar("T")


def _ordered_in_place(
    result: dict[int, Any],
    key: Callable[[Any], Any],
    reverse: bool,
) -> dict[int, Any]:
    """Order every list value of a batch-load result; leave scalars alone."""
    for value in result.values():
        if isinstance(value, list):
            value.sort(key=key, reverse=reverse)
    return result


class _OrderedLoader(DefaultIRelationLoader):
    """Sync loader that orders each loaded collection."""

    def __init__(self, descriptor: Any, key: Callable[[Any], Any], reverse: bool) -> None:
        super().__init__(descriptor)
        self._key = key
        self._reverse = reverse

    def batch_load(self, instances: list[Any], base_query: Any) -> dict[int, Any]:
        """Load every collection in one query, then order each of them."""
        result = super().batch_load(instances, base_query)
        return _ordered_in_place(result, self._key, self._reverse)


class _AsyncOrderedLoader(AsyncDefaultRelationLoader):
    """Async loader that orders each loaded collection."""

    def __init__(self, descriptor: Any, key: Callable[[Any], Any], reverse: bool) -> None:
        super().__init__(descriptor)
        self._key = key
        self._reverse = reverse

    async def batch_load(self, instances: list[Any], base_query: Any) -> dict[int, Any]:
        """Load every collection in one query, then order each of them."""
        result = await super().batch_load(instances, base_query)
        return _ordered_in_place(result, self._key, self._reverse)


class OrderedHasMany(HasMany, Generic[T]):
    """A ``HasMany`` whose collections are ordered by ``order_key``.

    Args:
        order_key: Sort key applied to each related record.
        reverse: Sort descending when true.
        **kwargs: Forwarded to :class:`HasMany` (``foreign_key``,
            ``inverse_of``, ``cache_config``).
    """

    def __init__(
        self,
        *,
        order_key: Callable[[Any], Any],
        reverse: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._loader = _OrderedLoader(self, order_key, reverse)


class AsyncOrderedHasMany(AsyncHasMany, Generic[T]):
    """Async twin of :class:`OrderedHasMany`."""

    def __init__(
        self,
        *,
        order_key: Callable[[Any], Any],
        reverse: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._loader = _AsyncOrderedLoader(self, order_key, reverse)


__all__ = ["AsyncOrderedHasMany", "OrderedHasMany"]
