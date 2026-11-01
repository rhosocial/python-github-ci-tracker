# tests/adapters/test_pool_health.py
"""``ci_status`` reports the connection pool, because a lost permit is invisible.

The 2026-10-30 incident looked like a healthy service: every probe answered, and
every request waited thirty seconds and then failed at ``acquire``. The figures
that gave it away were ``available = 0`` and ``in_use = 0`` at the same time --
a pool with capacity and no permits. Those belong where a caller asks "is the
service well".
"""

from __future__ import annotations

from types import SimpleNamespace

from rhosocial.activerecord.connection.pool import PoolStats

from rhosocial.github_ci_tracker.adapters.mcp.tools.status import _pool_payload


def _container(stats: PoolStats, max_size: int = 10) -> SimpleNamespace:
    pool = SimpleNamespace(get_stats=lambda: stats, config=SimpleNamespace(max_size=max_size))
    return SimpleNamespace(database=SimpleNamespace(pool=pool))


def test_a_healthy_pool_reports_its_numbers_without_a_hint() -> None:
    payload = _pool_payload(
        _container(PoolStats(current_available=8, current_in_use=2, total_timeouts=0))
    )

    assert payload is not None
    assert payload["in_use"] == 2
    assert payload["available"] == 8
    assert payload["max_size"] == 10
    assert payload["acquire_timeouts"] == 0
    assert "hint" not in payload


def test_no_permits_left_is_called_out_as_a_restart() -> None:
    """``available = 0`` with only the caller's own lease is not saturation.

    The call reading the pool holds a connection, so ``in_use`` is one in the
    shape the incident took; testing for zero would never fire.
    """
    payload = _pool_payload(
        _container(PoolStats(current_available=0, current_in_use=1, total_timeouts=412))
    )

    assert payload is not None
    assert payload["acquire_timeouts"] == 412
    assert "permits were lost" in payload["hint"]
    assert "Restart" in payload["hint"]


def test_a_fully_checked_out_pool_is_reported_as_saturation() -> None:
    payload = _pool_payload(_container(PoolStats(current_available=0, current_in_use=10)))

    assert payload is not None
    assert "checked out" in payload["hint"]


def test_no_database_means_no_pool_section() -> None:
    assert _pool_payload(SimpleNamespace()) is None
