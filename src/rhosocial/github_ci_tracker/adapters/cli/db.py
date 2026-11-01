# src/rhosocial/github_ci_tracker/adapters/cli/db.py
"""Database management CLI (rhosocial-github-ci-tracker-db)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Coroutine, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from rhosocial.activerecord.backend.impl.postgres.backend import AsyncPostgresBackend
from rhosocial.activerecord.backend.impl.postgres.config import PostgresConnectionConfig

from ...schema import TABLES, create_all, missing_tables, reset_all
from .eventloop import run_coroutine


def get_connection_config() -> PostgresConnectionConfig:
    """The connection coordinates, from the same settings the service reads.

    A maintenance tool that parsed ``CITRACKER_DB_*`` itself could drift from
    the service it is preparing a database for; there is one vocabulary of
    variables, and :class:`~rhosocial.github_ci_tracker.config.Settings` holds
    it.
    """
    from ...config import get_settings
    from ...database import Database

    return Database.connection_config(get_settings())


def _first_value(row: Any) -> Any:
    """Read the first column of a result row.

    Backends return rows either as tuples or as mappings; both are accepted.
    """
    if isinstance(row, Mapping):
        return next(iter(row.values()))
    return row[0]


def _run(coro: Coroutine[Any, Any, None]) -> None:
    """Run a coroutine on a loop psycopg accepts on Windows."""
    run_coroutine(coro)


async def cmd_init(args: argparse.Namespace) -> None:
    """Initialize database tables.

    By default the database itself is created first when the server does not
    have it: this project keeps no migrations, so a first deployment -- the
    container's init.d schema script included -- must be able to converge against
    a database name that does not exist yet. ``--no-create-database`` assumes it
    does.
    """
    config = get_connection_config()
    if not args.no_create_database:
        from ...database import PostgresServer

        created = await PostgresServer(config, args.maintenance_db).ensure()
        if created:
            print(f"Database '{config.database}' did not exist; created it.")
        else:
            print(f"Database '{config.database}' already exists.")
    backend = AsyncPostgresBackend(connection_config=config)
    try:
        await backend.connect()
        await create_all(backend)
        print("Database initialized successfully.")
    finally:
        await backend.disconnect()


async def cmd_reset(args: argparse.Namespace) -> None:
    """Drop and recreate every table.

    This project keeps no migrations: the DDL is the single source of truth, so
    a schema change is applied by rebuilding the database rather than by
    altering it in place.
    """
    backend = AsyncPostgresBackend(connection_config=get_connection_config())
    try:
        await backend.connect()
        if not args.yes:
            print("This drops every ct_* table and all stored CI history.")
            print("Re-run with --yes to confirm.")
            return
        await reset_all(backend)
        print("Database reset: all tables dropped and recreated.")
    finally:
        await backend.disconnect()


async def cmd_is_initialized(args: argparse.Namespace) -> None:
    """Exit 0 when the schema is present, 1 when this is a first boot.

    The container's init.d runner calls this before running any operational
    script. It has to answer without creating anything, so database existence is
    checked through the maintenance connection (connecting to an absent database
    fails at the driver) and then the tables are counted in the target database.
    """
    config = get_connection_config()

    from ...database import PostgresServer

    if not await PostgresServer(config, args.maintenance_db).exists():
        print(f"Database '{config.database}' does not exist; not initialized.")
        raise SystemExit(1)

    backend = AsyncPostgresBackend(connection_config=config)
    try:
        await backend.connect()
        missing = await missing_tables(backend)
        if missing:
            print(
                f"Database '{config.database}' is missing {len(missing)} table(s); "
                "not initialized."
            )
            raise SystemExit(1)
        print(f"Database '{config.database}' is initialized.")
    finally:
        await backend.disconnect()


async def cmd_drop(args: argparse.Namespace) -> None:
    """Drop the configured database after disconnecting its sessions.

    ``reset`` rebuilds the schema in place; this removes the database itself,
    which is what a teardown needs. The maintenance connection is separate, as
    PostgreSQL refuses to drop the database it is connected to.
    """
    config = get_connection_config()
    if not args.yes:
        print(f"This drops the database '{config.database}' and all stored CI history.")
        print("Re-run with --yes to confirm.")
        return

    from ...database import PostgresServer

    dropped = await PostgresServer(config, args.maintenance_db).drop()
    if dropped:
        print(f"Database '{config.database}' dropped.")
    else:
        print(f"Database '{config.database}' did not exist; nothing to drop.")


async def cmd_cleanup(args: argparse.Namespace) -> None:
    """Cleanup old data."""
    backend = AsyncPostgresBackend(connection_config=get_connection_config())
    try:
        await backend.connect()

        # The cutoff is computed locally and interpolated as a literal: this
        # backend's parameter preparation only accepts positional parameters
        # against the Postgres dialect (a named dict raises TypeError), and the
        # value comes from a validated day count, never from user text.
        if args.runs_before:
            days = int(args.runs_before.rstrip("d"))
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            result = await backend.execute(
                f"DELETE FROM ct_workflow_runs WHERE created_at < '{cutoff.isoformat()}'"
            )
            print(f"Deleted {result.affected_rows} old runs.")

        if args.logs_before:
            days = int(args.logs_before.rstrip("d"))
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            # Both log tables: step logs are the preferred copy and job logs the
            # fallback, and a purge that forgot one would leave rows nobody can
            # reach through the read path.
            for table in ("ct_step_logs", "ct_job_logs"):
                result = await backend.execute(
                    f"DELETE FROM {table} WHERE created_at < '{cutoff.isoformat()}'"
                )
                print(f"Deleted {result.affected_rows} old rows from {table}.")

        if getattr(args, "deliveries_before", None):
            days = int(args.deliveries_before.rstrip("d"))
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            # Only the ledger rows go: the deliveries themselves are already
            # applied to the run/job rows, and GitHub keeps its own copy for 30
            # days if one needs to be inspected again.
            result = await backend.execute(
                f"DELETE FROM ct_webhook_delivery WHERE received_at < '{cutoff.isoformat()}'"
            )
            print(f"Deleted {result.affected_rows} old webhook deliveries.")
    finally:
        await backend.disconnect()


async def cmd_stats(args: argparse.Namespace) -> None:
    """Show database statistics."""
    backend = AsyncPostgresBackend(connection_config=get_connection_config())
    try:
        await backend.connect()

        print("Database Statistics:")
        print("-" * 40)
        for table in TABLES:
            result = await backend.execute(f"SELECT COUNT(*) AS row_count FROM {table}")
            rows = result.data or []
            count = _first_value(rows[0]) if rows else 0
            print(f"  {table}: {count} rows")
    finally:
        await backend.disconnect()


async def cmd_deliveries(args: argparse.Namespace) -> None:
    """Show webhook deliveries, newest first.

    The ledger is the only place a dropped delivery is visible: when storing
    what a delivery described fails, no run or job row is left behind to
    notice. Each row carries the GitHub ids, so a failing delivery can be
    looked up on GitHub (``delivery_id``) and traced to the run/job it
    described without storing the body.
    """
    conditions = []
    if getattr(args, "unhandled", False):
        conditions.append("handled = FALSE")
    if getattr(args, "stream", None):
        conditions.append(f"event_type = '{args.stream}'")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    limit = max(1, int(getattr(args, "limit", 20) or 20))

    backend = AsyncPostgresBackend(connection_config=get_connection_config())
    try:
        await backend.connect()
        result = await backend.execute(
            "SELECT row_to_json(t) AS row FROM ct_webhook_delivery t "
            f"{where} ORDER BY received_at DESC LIMIT {limit}"
        )
        rows = result.data or []
        data = [
            (
                _first_value(row)
                if isinstance(_first_value(row), dict)
                else json.loads(_first_value(row))
            )
            for row in rows
        ]
        if not data:
            print("No matching deliveries.")
            return
        for row in data:
            print(
                f"{row.get('received_at')}  {row.get('event_type')}  "
                f"{row.get('repository_full_name') or '-'}  "
                f"run={row.get('github_run_id') or '-'}  job={row.get('github_job_id') or '-'}  "
                f"handled={row.get('handled')}  reason={row.get('reason') or '-'}"
            )
            print(f"  delivery={row.get('delivery_id') or '-'}")
    finally:
        await backend.disconnect()


async def cmd_export(args: argparse.Namespace) -> None:
    """Export table data as JSON.

    ``row_to_json`` is used so column names come back from PostgreSQL itself
    instead of being reconstructed from a cursor description.
    """
    backend = AsyncPostgresBackend(connection_config=get_connection_config())
    try:
        await backend.connect()
        result = await backend.execute(f"SELECT row_to_json(t) AS row FROM {args.table} t")
        rows = result.data or []
        # psycopg returns the JSON column already parsed as a dict; a plain
        # driver would hand back the text, so both shapes are accepted.
        data = [
            (
                _first_value(row)
                if isinstance(_first_value(row), dict)
                else json.loads(_first_value(row))
            )
            for row in rows
        ]

        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)

        print(f"Exported {len(data)} rows to {args.output}")
    finally:
        await backend.disconnect()


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="rhosocial-github-ci-tracker-db",
        description="CI Tracker database management CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    # init
    init_parser = subparsers.add_parser(
        "init", help="Initialize database tables (creates the database itself)"
    )
    init_parser.add_argument(
        "--no-create-database",
        action="store_true",
        help="Assume the database exists; never create it",
    )
    init_parser.add_argument(
        "--maintenance-db",
        default="postgres",
        help="Existing database to connect through when creating (default: postgres)",
    )

    # is-initialized
    is_initialized_parser = subparsers.add_parser(
        "is-initialized",
        help="Exit 0 when every ct_* table exists, 1 when the schema is absent",
    )
    is_initialized_parser.add_argument(
        "--maintenance-db",
        default="postgres",
        help="Existing database to connect through when checking (default: postgres)",
    )

    # reset
    reset_parser = subparsers.add_parser(
        "reset", help="Drop and recreate every table (no migrations)"
    )
    reset_parser.add_argument("--yes", action="store_true", help="Confirm dropping all ct_* tables")

    # drop
    drop_parser = subparsers.add_parser(
        "drop", help="Drop the configured database itself (teardown)"
    )
    drop_parser.add_argument("--yes", action="store_true", help="Confirm dropping the database")
    drop_parser.add_argument(
        "--maintenance-db",
        default="postgres",
        help="Existing database to connect through when dropping (default: postgres)",
    )

    # cleanup
    cleanup_parser = subparsers.add_parser("cleanup", help="Cleanup old data")
    cleanup_parser.add_argument(
        "--runs-before", type=str, help="Delete runs before N days (e.g., 90d)"
    )
    cleanup_parser.add_argument(
        "--logs-before", type=str, help="Delete logs before N days (e.g., 365d)"
    )
    cleanup_parser.add_argument(
        "--deliveries-before",
        type=str,
        help="Delete webhook delivery rows before N days (e.g., 30d); the applied "
        "state in runs/jobs is untouched",
    )

    # stats
    subparsers.add_parser("stats", help="Show database statistics")

    # export
    export_parser = subparsers.add_parser("export", help="Export table data as JSON")
    export_parser.add_argument("--table", required=True, help="Table name")
    export_parser.add_argument("--output", required=True, help="Output file")

    # deliveries: the webhook ledger, where a dropped delivery is only visible.
    deliveries_parser = subparsers.add_parser("deliveries", help="Show webhook deliveries")
    deliveries_parser.add_argument(
        "--unhandled", action="store_true", help="Only deliveries this service did not store"
    )
    deliveries_parser.add_argument(
        "--stream", choices=["workflow_run", "workflow_job"], help="Only this stream"
    )
    deliveries_parser.add_argument("--limit", type=int, default=20, help="Rows to show")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "init":
        _run(cmd_init(args))
    elif args.command == "is-initialized":
        _run(cmd_is_initialized(args))
    elif args.command == "reset":
        _run(cmd_reset(args))
    elif args.command == "drop":
        _run(cmd_drop(args))
    elif args.command == "cleanup":
        _run(cmd_cleanup(args))
    elif args.command == "stats":
        _run(cmd_stats(args))
    elif args.command == "export":
        _run(cmd_export(args))
    elif args.command == "deliveries":
        _run(cmd_deliveries(args))


if __name__ == "__main__":
    main()
