# src/rhosocial/github_ci_tracker/schema/__init__.py
"""Database schema management.

Two renderings of one definition (:mod:`schema.model`):

* :mod:`schema.ddl` -- plain SQL text, for review and as the portable fallback;
* :mod:`schema.ddl_postgres` -- built through the Postgres expression system,
  which is what the service executes.

:func:`create_all` and :func:`drop_all` are the entry points; ``create_all``
renders through the expression system, and the drop path emits plain
``DROP TABLE`` text because a drop needs no builder.

This package does not import ``credentials``: the provider tables are part of
:data:`SCHEMA` as data, and the provider reads its own table from here. The
dependency runs one way -- provider -> schema -- which is what keeps the core
unaware of any provider's columns.
"""

from __future__ import annotations

from collections.abc import Mapping

from . import ddl, ddl_postgres
from .model import (
    CORE_SCHEMA,
    PAT_SCHEMA,
    SCHEMA,
    Column,
    ColumnType,
    ForeignKey,
    Index,
    OnDelete,
    Schema,
    Table,
    UniqueConstraint,
)

#: Every table, dependants first: the order a teardown must delete in.
TABLES: tuple[str, ...] = SCHEMA.drop_order()

#: Retired table names, dropped on reset so an upgraded database keeps no
#: orphans. This project has no migrations.
LEGACY_TABLES: tuple[str, ...] = SCHEMA.legacy_tables

#: Provider-owned table names, appended after the core ones.
PROVIDER_TABLES: tuple[str, ...] = tuple(table.name for table in SCHEMA.provider_tables)


async def create_all(backend) -> None:
    """Create every table and index, through the Postgres expression system."""
    await ddl_postgres.create_all(backend, SCHEMA)


async def drop_all(backend) -> None:
    """Drop every table of this schema, legacy names included."""
    for statement in ddl.drop_statements(SCHEMA):
        await backend.execute(statement)


async def reset_all(backend) -> None:
    """Drop and recreate the whole schema.

    This project keeps no migrations: a schema change is applied by rebuilding
    the database, which is what ``rhosocial-github-ci-tracker-db reset`` does.
    """
    await drop_all(backend)
    await create_all(backend)


async def missing_tables(backend) -> tuple[str, ...]:
    """Return the tables of this schema that the connected database lacks.

    This is the first-boot test used by the container's init.d runner and by
    ``rhosocial-github-ci-tracker-db is-initialized``: a database counts as
    initialized once every table exists. Partial schemas are reported in full,
    so a re-run of ``create_all`` is what repairs them.
    """
    result = await backend.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    )
    present = {
        (next(iter(row.values())) if isinstance(row, Mapping) else row[0])
        for row in (result.data or [])
    }
    return tuple(name for name in TABLES if name not in present)


def render_sql() -> str:
    """Render the schema as one plain-SQL script (human review, portability)."""
    return ddl.render_schema(SCHEMA)


__all__ = [
    "CORE_SCHEMA",
    "LEGACY_TABLES",
    "PAT_SCHEMA",
    "PROVIDER_TABLES",
    "SCHEMA",
    "TABLES",
    "Column",
    "ColumnType",
    "ForeignKey",
    "Index",
    "OnDelete",
    "Schema",
    "Table",
    "UniqueConstraint",
    "create_all",
    "ddl",
    "ddl_postgres",
    "drop_all",
    "missing_tables",
    "render_sql",
    "reset_all",
]
