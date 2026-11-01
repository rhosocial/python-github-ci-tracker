# tests/schema/test_ddl_renderers.py
"""The schema has one definition and two renderings; they must agree.

This is the test that turns "the Postgres expression system can express this
schema" from a claim into a check. Both renderers read :mod:`schema.model`; if
one of them cannot express a construct the model declares, it either raises or
produces different SQL, and that is what fails here.

The comparison is deliberately structural rather than textual: the two renderers
spell identifiers and spacing the same way today, but the thing that matters is
that both produce the same tables, columns, constraints and indexes. A textual
diff would fail on a cosmetic change and pass on a semantic one -- the wrong way
round.
"""

from __future__ import annotations

import re

import pytest

from rhosocial.github_ci_tracker.schema import ddl, ddl_postgres
from rhosocial.github_ci_tracker.schema.model import (
    SCHEMA,
    ColumnType,
    OnDelete,
    Table,
)

#: Every table this schema defines, core and provider alike.
EXPECTED_TABLES = (
    "ct_repositories",
    "ct_repository_bindings",
    "ct_workflows",
    "ct_workflow_runs",
    "ct_run_attempts",
    "ct_jobs",
    "ct_steps",
    "ct_step_logs",
    "ct_job_logs",
    "ct_webhook_delivery",
    "ct_pat_credentials",
)


def _rendered() -> tuple[list[str], list[str]]:
    """Return (plain statements, expression statements)."""
    plain = [
        statement.strip() for statement in ddl.render_schema(SCHEMA).split(";") if statement.strip()
    ]
    return plain, ddl_postgres.render_schema(SCHEMA)


def _created_tables(statements: list[str]) -> list[str]:
    """Extract table names from ``CREATE TABLE`` statements, in order."""
    names = []
    for statement in statements:
        match = re.match(r'CREATE TABLE IF NOT EXISTS "?([\w]+)"? \(', statement)
        if match:
            names.append(match.group(1))
    return names


def _created_indexes(statements: list[str]) -> list[tuple[str, str]]:
    """Extract ``(index name, table name)`` from ``CREATE INDEX`` statements."""
    found = []
    for statement in statements:
        match = re.match(
            r'CREATE (?:UNIQUE )?INDEX IF NOT EXISTS "?([\w]+)"? ON "?([\w]+)"?',
            statement,
        )
        if match:
            found.append((match.group(1), match.group(2)))
    return found


def test_both_renderers_create_the_same_tables() -> None:
    """Same tables, in the same order, from one definition."""
    plain, expression = _rendered()
    assert _created_tables(plain) == list(EXPECTED_TABLES)
    assert _created_tables(expression) == list(EXPECTED_TABLES)


def test_both_renderers_create_the_same_indexes() -> None:
    """Same indexes on the same tables."""
    plain, expression = _rendered()
    assert sorted(_created_indexes(plain)) == sorted(_created_indexes(expression))
    assert len(_created_indexes(expression)) == 14


def test_expression_rendering_never_binds_parameters() -> None:
    """A schema statement must be self-contained.

    ``to_sql()`` returns ``(sql, params)``; params for DDL would mean something
    was rendered as a value that should have been inline -- the failure mode that
    turns ``DEFAULT NOW()`` into ``DEFAULT 'NOW()'``.
    """
    for statement in ddl_postgres.render_schema(SCHEMA):
        assert "%s" not in statement and ":1" not in statement


def test_expression_rendering_keeps_function_defaults_inline() -> None:
    """A timestamp default stays a call, never a quoted string.

    The exact spelling is the expression system's choice -- ``CURRENT_TIMESTAMP``
    and ``NOW()`` are both correct. What must never happen is the call being
    quoted, which would store the four characters ``NOW(`` as a string.
    """
    rendered = "\n".join(ddl_postgres.render_schema(SCHEMA))
    assert "DEFAULT CURRENT_TIMESTAMP" in rendered or "DEFAULT NOW()" in rendered
    assert "'CURRENT_TIMESTAMP'" not in rendered
    assert "'NOW()'" not in rendered


def test_expression_rendering_keeps_cascade_actions() -> None:
    """Every cascading key survives the expression path."""
    rendered = "\n".join(ddl_postgres.render_schema(SCHEMA))
    cascades = sum(
        1
        for table in SCHEMA.all_tables()
        for key in table.foreign_keys
        if key.on_delete is OnDelete.CASCADE
    )
    # binding, workflows, runs, jobs, steps, step_logs, job_logs
    assert cascades == 8
    assert rendered.count("ON DELETE CASCADE") == cascades


def test_plain_rendering_marks_every_foreign_key() -> None:
    """The portable text also spells each cascading key."""
    plain = ddl.render_schema(SCHEMA)
    assert plain.count("ON DELETE CASCADE") == 8


@pytest.mark.parametrize(
    ("column_type", "expected"),
    [
        (ColumnType.SERIAL, "SERIAL"),
        (ColumnType.INTEGER, "INTEGER"),
        (ColumnType.BIGINT, "BIGINT"),
        (ColumnType.TEXT, "TEXT"),
        (ColumnType.BOOLEAN, "BOOLEAN"),
        (ColumnType.TIMESTAMPTZ, "TIMESTAMPTZ"),
        (ColumnType.JSONB, "JSONB"),
    ],
)
def test_every_column_type_renders_in_both(column_type: ColumnType, expected: str) -> None:
    """Each type the model can declare is expressible by both renderers.

    A type that exists in the model but not in a renderer would only surface when
    a migration-free rebuild hit it in production.
    """
    from rhosocial.github_ci_tracker.schema.model import Column

    column = Column("probe", column_type, nullable=False)
    assert expected in ddl.render_column(column)

    # The expression path builds the type node; a missing mapping raises here.
    node = ddl_postgres._data_type(column)
    assert node is not None


def test_varchar_length_is_honoured() -> None:
    """``VARCHAR(n)`` keeps its length in both renderings."""
    from rhosocial.github_ci_tracker.schema.model import Column

    column = Column("probe", ColumnType.VARCHAR, length=42)
    assert "VARCHAR(42)" in ddl.render_column(column)
    assert "VARCHAR(42)" in ddl_postgres.render_create_table(
        Table(
            name="t",
            columns=(
                Column("id", ColumnType.SERIAL, primary_key=True, nullable=False),
                column,
            ),
        )
    )


def test_drop_order_covers_every_table_and_legacy_name() -> None:
    """A reset must drop everything, including names this schema retired."""
    dropped = " ".join(ddl.drop_statements(SCHEMA))
    for name in EXPECTED_TABLES:
        assert f'"{name}"' in dropped
    # Retired tables: leaving them behind would strand rows from an old shape.
    # ``ct_step_logs`` used to be listed here too; it is a live table again, and
    # the loop above already covers it.
    assert '"ct_github_tokens"' in dropped
