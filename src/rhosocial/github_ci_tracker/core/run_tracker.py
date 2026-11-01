# src/rhosocial/github_ci_tracker/core/run_tracker.py
"""In-memory tracking of the runs this system triggered.

GitHub webhook events are delivered for *every* run of a repository, including
runs started manually or by another actor. :class:`RunTracker` keeps the set of
``github_run_id`` values produced by
:class:`~rhosocial.github_ci_tracker.controllers.dispatch.DispatchController`
so webhook handlers can ignore foreign events (see ``10.1 事件过滤策略`` in the
core services plan).

The set is bounded: entries are evicted in insertion order once ``max_size`` is
reached, which keeps memory usage predictable for long running processes.

Only the class lives here. The instance a process shares between the trigger and
webhook services is in :mod:`rhosocial.github_ci_tracker.controllers.run_tracker`:
sharing policy is composition, and a process-wide mutable global in the domain
layer would make tests order-dependent.
"""

from __future__ import annotations

from collections import OrderedDict


class RunTracker:
    """Track ``github_run_id`` values triggered by this system."""

    def __init__(self, max_size: int = 10_000) -> None:
        """Initialize the tracker.

        Args:
            max_size: Maximum number of tracked run ids. When exceeded the
                oldest entries are dropped first.
        """
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._max_size = max_size
        self._tracked: OrderedDict[int, None] = OrderedDict()

    def add(self, github_run_id: int) -> None:
        """Mark a run as triggered by this system."""
        self._tracked[github_run_id] = None
        while len(self._tracked) > self._max_size:
            self._tracked.popitem(last=False)

    def remove(self, github_run_id: int) -> None:
        """Stop tracking a run (e.g. once it reached a terminal state)."""
        self._tracked.pop(github_run_id, None)

    def discard(self, github_run_id: int) -> None:
        """Alias of :meth:`remove`, mirroring ``set.discard`` semantics."""
        self.remove(github_run_id)

    def is_tracked(self, github_run_id: int) -> bool:
        """Return ``True`` when the run was triggered by this system."""
        return github_run_id in self._tracked

    def clear(self) -> None:
        """Forget every tracked run."""
        self._tracked.clear()

    def __contains__(self, github_run_id: object) -> bool:
        return github_run_id in self._tracked

    def __len__(self) -> int:
        return len(self._tracked)

    def __repr__(self) -> str:
        return f"RunTracker(tracked={len(self._tracked)}, max_size={self._max_size})"


__all__ = ["RunTracker"]
