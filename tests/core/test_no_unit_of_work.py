# tests/core/test_no_unit_of_work.py
"""The unit-of-work idea stays out of the source, and a test keeps it out.

This project is built on ActiveRecord: one ``save()`` is one complete write, and
the model that owns a row is the only thing that writes it. A unit of work --
"these models commit together, so let me mark the set, open a transaction over
them and roll back as a group" -- is the opposite arrangement. It also arrives in
disguise, which is why the list below is explicit: the module, the scope helper,
the lock that exists only to keep two writers out of one transaction, the
deferred work that waits for a commit, and the diagnostics that read the
transaction manager's level.

The direction of repair is the opposite of a transaction: a delivery that stops
halfway leaves the rows it reached, the ledger records that it was not handled,
and the next read asks GitHub -- the source for everything that can change -- and
repairs the rest. The phrase may appear in prose; the names may not appear at all.

If this test fails, the offending name is a leftover: remove it, do not move it.
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "rhosocial" / "github_ci_tracker"

#: Names that only make sense with a unit of work behind them.
#:
#: The transaction surface is listed in full, not just ``.transaction(``: every
#: way to open, end or batch a multi-row unit is the same idea wearing another
#: name, and the one time this project opened transactions it produced
#: ``TransactionError: No active transaction to rollback`` on duplicate
#: deliveries -- a rollback error masking the real one. A single ``save()`` is
#: the atomic unit here; anything that groups writes needs its own argument.
BANNED = (
    "unit_of_work",
    "UnitOfWork",
    "write_scope",
    "delivery_lock",
    "flush_pending_log_fetches",
    "recent_bindings",
    ".transaction(",
    "transaction_manager",
    "begin_transaction",
    "rollback_transaction",
    "commit_transaction",
    "bulk_create",
    "bulk_update",
    "bulk_delete",
)


def _hits(source: str) -> list[str]:
    """Every banned name in ``source``, with the line that carries it."""
    return [
        f"line {number}: {name}"
        for number, line in enumerate(source.splitlines(), start=1)
        for name in BANNED
        if name in line
    ]


def _offenders(root: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        hits = _hits(path.read_text(encoding="utf-8"))
        if hits:
            found[str(path.relative_to(SRC))] = hits
    return found


def test_the_source_carries_no_unit_of_work() -> None:
    offenders = _offenders(SRC)

    assert not offenders, "unit-of-work leftovers in the source:\n" + "\n".join(
        f"  {path}: {', '.join(hits)}" for path, hits in offenders.items()
    )


def test_the_scan_finds_the_names_it_exists_for() -> None:
    """Guard the guard: a scanner that reports nothing proves nothing."""
    source = (
        "from .unit_of_work import write_scope\n"
        "async with delivery_lock():\n"
        "    async with self._pool.transaction():\n"
        "        pass\n"
        "print(self._database.recent_bindings())\n"
        "manager = backend.transaction_manager\n"
        "await backend.rollback_transaction()\n"
        "await AsyncStep.bulk_create(rows)\n"
    )

    assert _hits(source) == [
        "line 1: unit_of_work",
        "line 1: write_scope",
        "line 2: delivery_lock",
        "line 3: .transaction(",
        "line 5: recent_bindings",
        "line 6: transaction_manager",
        "line 7: rollback_transaction",
        "line 8: bulk_create",
    ]
