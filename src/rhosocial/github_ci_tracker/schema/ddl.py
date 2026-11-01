# src/rhosocial/github_ci_tracker/schema/ddl.py
"""Plain-SQL rendering of the schema.

This is the portable rendering: it emits SQL text for the definitions in
:mod:`schema.model` without using any backend's expression system. Two reasons it
still exists alongside the expression renderer:

* it is what a human reviews when the schema changes -- a diff of SQL text says
  more at a glance than a diff of constructor calls;
* it is the fallback for a backend whose expression support is incomplete. The
  service runs on PostgreSQL today, but a schema this small does not need to be
  welded to one dialect's builder.

The output is not executed by the service; :mod:`schema.ddl_postgres` is. The
two are pinned against each other by a test that renders both and compares their
structure, so "the expression system can express this schema" is a checked claim
rather than an aspiration.

Defaults arrive as Python values (see :class:`~schema.model.Column`), so spelling
them is this renderer's job: a bool becomes ``TRUE``/``FALSE``, a string is
quoted, a number stays bare, and :class:`~schema.model.DefaultNow` becomes the
dialect's spelling of "now".
"""

from __future__ import annotations

from .model import Column, ColumnType, DefaultNow, Schema, Table

#: SQL type names per dialect-agnostic spelling. The Postgres names are used
#: because they are also what the expression renderer produces, which keeps the
#: two renderings comparable.
_TYPE_SQL: dict[ColumnType, str] = {
    ColumnType.SERIAL: "SERIAL",
    ColumnType.INTEGER: "INTEGER",
    ColumnType.BIGINT: "BIGINT",
    ColumnType.VARCHAR: "VARCHAR",
    ColumnType.TEXT: "TEXT",
    ColumnType.BOOLEAN: "BOOLEAN",
    ColumnType.TIMESTAMPTZ: "TIMESTAMPTZ",
    ColumnType.JSONB: "JSONB",
}


def _quote(identifier: str) -> str:
    """Quote an identifier, escaping embedded double quotes."""
    return '"' + identifier.replace('"', '""') + '"'


def _quote_value(value: str) -> str:
    """Quote a string literal, escaping embedded single quotes."""
    return "'" + value.replace("'", "''") + "'"


def render_default(column: Column) -> str | None:
    """Render a column's default, or ``None`` when it has none.

    Args:
        column: The column definition.

    Returns:
        The default clause value, spelled for SQL. The caller prefixes
        ``DEFAULT``.

    Raises:
        TypeError: The default is a type this renderer does not know. Raising is
            better than stringifying it: a stray ``<object>`` would reach the
            database as a syntax error during a rebuild.
    """
    value = column.default
    if value is None:
        return None
    if isinstance(value, DefaultNow):
        return "NOW()"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _quote_value(value)
    raise TypeError(
        f"column '{column.name}' has a default of type {type(value).__name__}, "
        "which this renderer cannot spell"
    )


def render_type(column: Column) -> str:
    """Render a column's SQL type."""
    sql = _TYPE_SQL[column.type]
    if column.type is ColumnType.VARCHAR and column.length:
        return f"{sql}({column.length})"
    return sql


def render_column(column: Column) -> str:
    """Render one column definition, constraints included."""
    parts = [_quote(column.name), render_type(column)]
    if column.primary_key:
        parts.append("PRIMARY KEY")
    if not column.nullable and not column.primary_key:
        parts.append("NOT NULL")
    if column.unique:
        parts.append("UNIQUE")
    default = render_default(column)
    if default is not None:
        parts.append(f"DEFAULT {default}")
    return " ".join(parts)


def render_create_table(table: Table, schema: Schema | None = None) -> str:
    """Render one ``CREATE TABLE IF NOT EXISTS`` statement.

    Args:
        table: The table definition.
        schema: Reserved for future cross-schema qualification; unused today.

    Returns:
        The statement text, without a trailing semicolon.
    """
    del schema
    clauses = [render_column(column) for column in table.columns]

    for key in table.foreign_keys:
        clause = (
            f"FOREIGN KEY ({_quote(key.column)}) "
            f"REFERENCES {_quote(key.references_table)}({_quote(key.references_column)})"
        )
        if key.on_delete is not None:
            clause += f" ON DELETE {key.on_delete.value}"
        clauses.append(clause)

    for unique in table.unique_constraints:
        columns = ", ".join(_quote(name) for name in unique.columns)
        clauses.append(f"UNIQUE ({columns})")

    body = ", ".join(clauses)
    return f"CREATE TABLE IF NOT EXISTS {_quote(table.name)} ({body})"


def render_create_index(table: Table) -> list[str]:
    """Render one table's standalone index statements."""
    statements = []
    for index in table.indexes:
        columns = ", ".join(_quote(name) for name in index.columns)
        unique = "UNIQUE " if index.unique else ""
        statements.append(
            f"CREATE {unique}INDEX IF NOT EXISTS {_quote(index.name)} "
            f"ON {_quote(table.name)} ({columns})"
        )
    return statements


def render_schema(schema: Schema) -> str:
    """Render the whole schema as one executable script.

    Args:
        schema: The schema to render.

    Returns:
        Every ``CREATE TABLE`` and ``CREATE INDEX``, separated by blank lines.
    """
    statements: list[str] = []
    for table in schema.all_tables():
        statements.append(render_create_table(table, schema))
        statements.extend(render_create_index(table))
    return ";\n\n".join(statements) + ";\n"


def drop_statements(schema: Schema) -> list[str]:
    """Render the ``DROP TABLE`` statements, dependants first, legacy included."""
    names = (*schema.drop_order(), *schema.legacy_tables)
    return [f"DROP TABLE IF EXISTS {_quote(name)} CASCADE" for name in names]


__all__ = [
    "drop_statements",
    "render_column",
    "render_create_index",
    "render_create_table",
    "render_default",
    "render_schema",
    "render_type",
]
