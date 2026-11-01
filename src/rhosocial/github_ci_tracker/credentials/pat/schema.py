# src/rhosocial/github_ci_tracker/credentials/pat/schema.py
"""Schema contribution of the PAT provider.

The table is defined once, in :mod:`schema.model` (``PAT_SCHEMA``), so both
schema renderers see it. This module is the seam the provider registry reads:
it names the table this provider owns and exposes the rendered statements for
tooling that wants a provider in isolation.

Keeping the definition in one place is what stops the provider's table from
drifting out of the schema that ``reset`` actually builds.
"""

from __future__ import annotations

from ...schema.ddl import render_create_index, render_create_table
from ...schema.ddl_postgres import render_create_index as render_index_expr
from ...schema.ddl_postgres import render_create_table as render_table_expr
from ...schema.model import PAT_SCHEMA

#: The table this provider owns.
TABLE = PAT_SCHEMA

#: Table names this provider owns, for teardown ordering.
TABLES: tuple[str, ...] = (PAT_SCHEMA.name,)

#: Plain-SQL rendering of this provider's table and indexes.
DDL: str = "\n\n".join([render_create_table(PAT_SCHEMA), *render_create_index(PAT_SCHEMA)])

#: Expression-system rendering, which is what the service executes.
DDL_STATEMENTS: tuple[str, ...] = (
    render_table_expr(PAT_SCHEMA),
    *render_index_expr(PAT_SCHEMA),
)

__all__ = ["DDL", "DDL_STATEMENTS", "TABLE", "TABLES"]
