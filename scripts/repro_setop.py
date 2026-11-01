"""Reproduce the SetOperationQuery parenthesization defect on SQLite.

No server needed: the activerecord core ships a SQLite backend, and the
ORDER BY/LIMIT inside UNION branches is a pure SQL-rendering defect.

Repro (four steps):
  1. configure a model on a temp SQLite file
  2. create + seed the table through the backend (raw SQL)
  3. build two per-workflow ORDER BY ... LIMIT 1 branches, UNION ALL them
  4. print the rendered SQL, then execute and capture the failure
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def main() -> None:
    from typing import ClassVar

    from rhosocial.activerecord.backend.impl.sqlite.backend import SQLiteBackend
    from rhosocial.activerecord.backend.impl.sqlite.config import SQLiteConnectionConfig
    from rhosocial.activerecord.base.field_proxy import FieldProxy
    from rhosocial.activerecord.model import ActiveRecord

    class Run(ActiveRecord):
        __table_name__ = "runs"
        __pk_auto_generated__ = True
        c: ClassVar[FieldProxy] = FieldProxy()

        id: int | None = None
        workflow_id: int = 0
        run_number: int = 0
        status: str = "queued"

    db_file = Path(tempfile.gettempdir()) / "ct_setop_repro.sqlite3"
    if db_file.exists():
        db_file.unlink()

    Run.configure(
        config=SQLiteConnectionConfig(database=str(db_file)),
        backend_class=SQLiteBackend,
    )
    backend = Run.backend()
    backend.connect()
    try:
        backend.execute(
            "CREATE TABLE IF NOT EXISTS runs ("
            "id INTEGER PRIMARY KEY, workflow_id INTEGER, run_number INTEGER, status TEXT)"
        )
        for wid, nums in ((1, (1, 2)), (2, (1, 2))):
            for n in nums:
                backend.execute(
                    "INSERT INTO runs (workflow_id, run_number, status) VALUES (?, ?, ?)",
                    (wid, n, "completed"),
                )

        # Build the compound: per-workflow ORDER BY ... LIMIT 1, UNION ALL
        branches = [
            Run.query()
            .where(Run.c.workflow_id == wid)
            .order_by((Run.c.run_number, "DESC"))
            .limit(1)
            for wid in (1, 2)
        ]
        from rhosocial.activerecord.query.set_operation import SetOperationQuery

        compound = SetOperationQuery(branches[0], branches[1], "UNION ALL")

        rendered = compound.to_sql()
        print("rendered SQL :")
        print(f"  {rendered[0]}")
        print(f"params       : {rendered[1]}")

        print("\nexecution    :")
        try:
            rows = compound.aggregate()
            print(f"  aggregate() returned {rows}")
        except Exception as exc:
            print(f"  FAILED: {type(exc).__name__}: {exc}")
    finally:
        backend.disconnect()
        db_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
