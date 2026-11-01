# tests/app/test_schema_state.py
"""The first-boot test used by the init.d runner.

``missing_tables`` must answer from the catalog alone -- no DDL, no writes --
because the entrypoint calls it on every start to decide whether this is the
first boot. Both row shapes the backends can return are accepted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from rhosocial.github_ci_tracker.schema import TABLES, missing_tables


class _Result:
    def __init__(self, data: Sequence[Any]) -> None:
        self.data = data


class _Backend:
    """Minimal stand-in: records the statement and returns fixed rows."""

    def __init__(self, rows: Sequence[Any]) -> None:
        self.rows = rows
        self.statements: list[str] = []

    async def execute(self, statement: str) -> _Result:
        self.statements.append(statement)
        return _Result(self.rows)


def test_a_complete_schema_has_nothing_missing() -> None:
    backend = _Backend([(name,) for name in TABLES])

    assert asyncio.run(missing_tables(backend)) == ()
    assert "information_schema.tables" in backend.statements[0]


def test_partial_schemas_are_reported_in_full() -> None:
    present = [name for name in TABLES if name != "ct_pat_credentials"]
    backend = _Backend([(name,) for name in present])

    assert asyncio.run(missing_tables(backend)) == ("ct_pat_credentials",)


def test_mapping_rows_are_accepted() -> None:
    backend = _Backend([{"table_name": name} for name in TABLES])

    assert asyncio.run(missing_tables(backend)) == ()
