# src/rhosocial/github_ci_tracker/schema/ddl_postgres.py
"""PostgreSQL rendering of the schema through the expression system.

The service executes *this*, not the plain-SQL text in :mod:`schema.ddl`. The
point is not novelty: the expression system is the backend's own statement
builder, so a table created through it is created by the same machinery that
formats queries, and a defect in that machinery shows up here rather than in a
string this project hand-maintains.

What this module demonstrates, one construct per helper below: the expression
system can express every feature this schema uses -- serial/identity columns,
typed columns with length, ``NOT NULL``/``DEFAULT``/``PRIMARY KEY``/``UNIQUE``,
single-column foreign keys with ``ON DELETE CASCADE``, table-level unique
constraints over column tuples, and standalone indexes. That is the whole
vocabulary of the schema in :mod:`schema.model`; if a new column needs a feature
beyond it, the model gains a construct and both renderers learn it.

Two details the renderer has to get right, both of which the plain-SQL renderer
gets for free by being text:

* a constraint must be wrapped in its production node -- ``ColumnConstraint``
  around a branch, ``TableConstraint`` around a table branch -- because the
  grammar puts the ``CONSTRAINT``-level traits there;
* ``DEFAULT NOW()`` is a call, not a string. Passing the text ``"NOW()"`` to the
  default branch renders a quoted literal, which silently becomes the string
  ``'NOW()'`` in the database.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

from .model import Column, ColumnType, DefaultNow, ForeignKey, Table

if TYPE_CHECKING:
    from rhosocial.activerecord.backend.impl.postgres.backend import AsyncPostgresBackend

#: The server version the dialect is built for. Only affects rendering of
#: version-gated syntax; nothing here is gated.
_POSTGRES_VERSION = (18, 0, 0)


@lru_cache(maxsize=1)
def dialect():
    """Return the Postgres dialect used to render this schema.

    Cached because constructing it walks the dialect's mixin chain, which is not
    free and never varies.
    """
    from rhosocial.activerecord.backend.impl.postgres.dialect import PostgresDialect

    return PostgresDialect(_POSTGRES_VERSION)


def _data_type(column: Column) -> Any:
    """Build the expression-system type node for a column.

    Raises:
        KeyError: The column type has no mapping; the schema and this table must
            be extended together.
    """
    from rhosocial.activerecord.backend.expression.types import (
        BigIntType,
        BooleanType,
        IntegerType,
        JsonBType,
        TextType,
        VarCharType,
    )
    from rhosocial.activerecord.backend.expression.types.datetime_ import TimestampTzType
    from rhosocial.activerecord.backend.impl.postgres.expression.types import (
        PostgresSerialType,
    )

    d = dialect()
    builders = {
        ColumnType.SERIAL: lambda: PostgresSerialType(d),
        ColumnType.INTEGER: lambda: IntegerType(d),
        ColumnType.BIGINT: lambda: BigIntType(d),
        ColumnType.VARCHAR: lambda: VarCharType(d, column.length or 255),
        ColumnType.TEXT: lambda: TextType(d),
        ColumnType.BOOLEAN: lambda: BooleanType(d),
        ColumnType.TIMESTAMPTZ: lambda: TimestampTzType(d),
        ColumnType.JSONB: lambda: JsonBType(d),
    }
    return builders[column.type]()


def _default_value(column: Column) -> Any:
    """Turn a column's declared default into an expression node.

    The default reaches SQL through the expression system's own value channel:
    a Python value passed to ``ColumnDefaultConstraint`` is wrapped in an
    ``inline=True`` literal by ``DefaultValueClause``, which is what a DDL
    position requires (it accepts no bind parameters). No raw SQL is involved --
    that is the point of using the builder at all.

    ``DefaultNow`` is the one case that is a call rather than a value, so it
    becomes a ``FunctionCall``. Passing the text ``"NOW()"`` as a string would
    have rendered a quoted literal that silently stops being a timestamp.

    Raises:
        TypeError: The default is a type the expression system has no value node
            for.
    """
    from rhosocial.activerecord.backend.expression.functions import current_timestamp

    value = column.default
    if isinstance(value, DefaultNow):
        return current_timestamp(dialect())
    if isinstance(value, (bool, int, float, str)):
        # A plain value: ColumnDefaultConstraint wraps it as an inline literal.
        return value
    raise TypeError(
        f"column '{column.name}' has a default of type {type(value).__name__}, "
        "which the expression system has no node for"
    )


def _column_definition(column: Column) -> Any:
    """Build a ``ColumnDefinition`` with its constraints wrapped as productions."""
    from rhosocial.activerecord.backend.expression.statements.ddl_table import (
        ColumnConstraint,
        ColumnDefaultConstraint,
        ColumnDefinition,
        ColumnNotNullConstraint,
        ColumnPrimaryKeyConstraint,
        ColumnUniqueConstraint,
    )

    d = dialect()
    constraints = []
    if column.primary_key:
        constraints.append(ColumnConstraint(d, branch=ColumnPrimaryKeyConstraint(d)))
    # A primary key is implicitly NOT NULL; spelling both is noise.
    elif not column.nullable:
        constraints.append(ColumnConstraint(d, branch=ColumnNotNullConstraint(d)))
    if column.unique:
        constraints.append(ColumnConstraint(d, branch=ColumnUniqueConstraint(d)))
    if column.default is not None:
        constraints.append(
            ColumnConstraint(d, branch=ColumnDefaultConstraint(d, _default_value(column)))
        )

    return ColumnDefinition(d, column.name, _data_type(column), constraints=constraints or None)


def _table_constraint(key: ForeignKey) -> Any:
    """Build a table-level ``FOREIGN KEY`` wrapped in its production node."""
    from rhosocial.activerecord.backend.expression.objects import Table
    from rhosocial.activerecord.backend.expression.statements.ddl_table import (
        ReferentialAction,
        TableConstraint,
        TableForeignKeyConstraint,
    )

    d = dialect()
    return TableConstraint(
        d,
        branch=TableForeignKeyConstraint(
            d,
            [key.column],
            (Table(d, key.references_table), [key.references_column]),
            on_delete=ReferentialAction(key.on_delete.value) if key.on_delete else None,
        ),
    )


def _unique_constraint(columns: tuple[str, ...]) -> Any:
    """Build a table-level ``UNIQUE`` over one or more columns."""
    from rhosocial.activerecord.backend.expression.statements.ddl_table import (
        TableConstraint,
        TableUniqueConstraint,
    )

    d = dialect()
    return TableConstraint(d, branch=TableUniqueConstraint(d, list(columns)))


def render_create_table(table: Table) -> str:
    """Render one ``CREATE TABLE IF NOT EXISTS`` through the expression system.

    Args:
        table: The table definition.

    Returns:
        The statement text, without a trailing semicolon.

    Raises:
        ValueError: The expression system emitted bind parameters. A DDL
            statement must be self-contained; parameters would mean something
            was rendered as a value that should have been inline.
    """
    from rhosocial.activerecord.backend.expression.objects import Table as TableNode
    from rhosocial.activerecord.backend.expression.statements.ddl_table import (
        CreateTableStatement,
    )

    d = dialect()
    statement = CreateTableStatement(
        d,
        table=TableNode(d, table.name),
        if_not_exists=True,
        columns=[_column_definition(column) for column in table.columns],
        table_constraints=[
            *(_table_constraint(key) for key in table.foreign_keys),
            *(_unique_constraint(unique.columns) for unique in table.unique_constraints),
        ],
    )
    sql, params = statement.to_sql()
    if params:
        raise ValueError(
            f"DDL for '{table.name}' produced bind parameters {params!r}; "
            "a schema statement must render inline"
        )
    return sql


def render_create_index(table: Table) -> list[str]:
    """Render one table's index statements through the expression system."""
    from rhosocial.activerecord.backend.expression.objects import Index as IndexNode
    from rhosocial.activerecord.backend.expression.objects import Table as TableNode
    from rhosocial.activerecord.backend.expression.statements.ddl_index import (
        CreateIndexStatement,
    )

    d = dialect()
    statements = []
    for index in table.indexes:
        sql, params = CreateIndexStatement(
            d,
            index=IndexNode(d, index.name),
            table=TableNode(d, table.name),
            columns=list(index.columns),
            unique=index.unique,
            if_not_exists=True,
        ).to_sql()
        if params:
            raise ValueError(f"index '{index.name}' produced bind parameters {params!r}")
        statements.append(sql)
    return statements


def render_schema(schema: Any) -> list[str]:
    """Render every table and index as individual statements.

    Statements are returned separately rather than as one script: the driver
    executes them one at a time, and a failure points at one statement.

    Args:
        schema: The schema to render.

    Returns:
        ``CREATE TABLE`` statements followed by their ``CREATE INDEX``
        statements, in schema order.
    """
    statements: list[str] = []
    for table in schema.all_tables():
        statements.append(render_create_table(table))
        statements.extend(render_create_index(table))
    return statements


async def create_all(backend: AsyncPostgresBackend, schema: Any) -> None:
    """Create every table and index on ``backend``."""
    from .model import SCHEMA as DEFAULT_SCHEMA

    for statement in render_schema(schema or DEFAULT_SCHEMA):
        await backend.execute(statement)


__all__ = [
    "create_all",
    "dialect",
    "render_create_index",
    "render_create_table",
    "render_schema",
]
