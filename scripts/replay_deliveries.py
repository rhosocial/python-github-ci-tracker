"""Replay recorded GitHub deliveries at the receiver, then check the invariants.

The point is not "does it hold under load" but "does concurrency change the
result". A replay is applied twice:

* **sequential** -- the capture, in order, one delivery at a time. Its final
  state is the reference: for the same ordered set of deliveries there is only
  one correct answer.
* **concurrent** (optionally shuffled, optionally duplicated) -- the same
  deliveries, overlapping. Its final state must equal the reference.

A difference means some delivery was lost, applied twice, or half-applied.
That is a much stronger check than "no exception was raised", because it does
not depend on knowing where the race is.

Usage
-----
    # 1. build the reference state
    replay_deliveries.py apply captured.jsonl --mode sequential
    replay_deliveries.py snapshot --output ref.json

    # 2. replay under concurrency (same scratch database!)
    replay_deliveries.py reset          # truncate the tables this tool inserts into
    replay_deliveries.py apply captured.jsonl --mode shuffled --concurrency 16 --repeat 2
    replay_deliveries.py snapshot --output concurrent.json

    # 3. compare and check invariants
    replay_deliveries.py compare ref.json concurrent.json
    replay_deliveries.py check captured.jsonl

Capture format (one JSON object per line)::

    {"delivery_id": "…", "event": "workflow_job", "body": {…}}
    {"delivery_id": "…", "event": "workflow_run", "body_path": "bodies/123.json"}

``body`` is inline; ``body_path`` is relative to the capture file. Both are
accepted, because GitHub's delivery API returns the parsed payload and a human
capturing it will usually write one file per delivery.

Safety
------
The tool writes to whatever database the environment points at, and ``reset``
truncates tables. It therefore refuses to run unless the database name contains
``replay`` or ``test``, or ``--allow-production`` is passed explicitly. Replay
into a scratch database: a capture applied to production leaves rows that no
future check can distinguish from real ones.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import random
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Tables the receiver writes to, in an order that can be truncated safely.
TRUNCATABLE = (
    "ct_webhook_delivery",
    "ct_steps",
    "ct_jobs",
    "ct_workflow_runs",
    "ct_workflows",
)


@dataclass
class Delivery:
    """One recorded delivery, ready to replay."""

    delivery_id: str
    event: str
    body: bytes


def load_capture(path: Path) -> list[Delivery]:
    """Read a JSONL capture into deliveries, resolving ``body_path``."""
    deliveries: list[Delivery] = []
    base = path.parent
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{number}: not JSON: {exc}") from exc

            event = record.get("event") or record.get("event_type")
            delivery_id = record.get("delivery_id") or ""
            if not event or not delivery_id:
                raise SystemExit(f"{path}:{number}: needs 'event' and 'delivery_id'")

            if "body" in record:
                body = json.dumps(record["body"]).encode()
            elif "body_path" in record:
                body = (base / record["body_path"]).read_bytes()
            else:
                raise SystemExit(f"{path}:{number}: needs 'body' or 'body_path'")

            deliveries.append(Delivery(delivery_id=delivery_id, event=event, body=body))
    return deliveries


def sign(secret: str, body: bytes) -> str:
    """GitHub's ``X-Hub-Signature-256`` value."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _database_url() -> str:
    return (
        f"{os.environ.get('CITRACKER_DB_HOST', '')}:{os.environ.get('CITRACKER_DB_PORT', '')}"
        f"/{os.environ.get('CITRACKER_DB_NAME', '')}"
    )


def _refuse_unless_scratch(database: str, allow_production: bool) -> None:
    if allow_production:
        return
    lowered = database.lower()
    if "replay" in lowered or "test" in lowered:
        return
    raise SystemExit(
        f"refusing to write to database {database!r}: replay into a scratch database "
        "(a name containing 'replay' or 'test') or pass --allow-production"
    )


def connect() -> Any:
    """Open the database the environment points at."""
    import psycopg

    database = os.environ.get("CITRACKER_DB_NAME", "")
    return psycopg.connect(
        host=os.environ.get("CITRACKER_DB_HOST", ""),
        port=int(os.environ.get("CITRACKER_DB_PORT", "5432")),
        dbname=database,
        user=os.environ.get("CITRACKER_DB_USER", ""),
        password=os.environ.get("CITRACKER_DB_PASSWORD", ""),
        sslmode=os.environ.get("CITRACKER_DB_SSLMODE", "prefer"),
        connect_timeout=10,
    )


# --------------------------------------------------------------------------- #
# applying a capture
# --------------------------------------------------------------------------- #


async def deliver(
    client: Any,
    base_url: str,
    secret: str,
    delivery: Delivery,
    headers_extra: dict[str, str] | None = None,
) -> tuple[str, int, str]:
    """POST one delivery; returns ``(delivery_id, status, body[:200])``."""
    headers = {
        "X-GitHub-Event": delivery.event,
        "X-GitHub-Delivery": delivery.delivery_id,
        "X-Hub-Signature-256": sign(secret, delivery.body),
        "Content-Type": "application/json",
    }
    if headers_extra:
        headers.update(headers_extra)
    response = await client.post(
        f"{base_url.rstrip('/')}/webhooks/github", content=delivery.body, headers=headers
    )
    return delivery.delivery_id, response.status_code, response.text[:200]


async def apply_capture(
    deliveries: Sequence[Delivery],
    *,
    base_url: str,
    secret: str,
    concurrency: int,
    repeat: int,
    shuffle: bool,
    delay: float,
) -> list[tuple[str, int, str]]:
    """Replay ``deliveries``, ``repeat`` times, in the requested order."""
    import httpx

    ordered = list(deliveries) * max(1, repeat)
    if shuffle:
        random.shuffle(ordered)

    results: list[tuple[str, int, str]] = []
    async with httpx.AsyncClient(verify=False, timeout=120) as client:
        if concurrency <= 1:
            for delivery in ordered:
                results.append(await deliver(client, base_url, secret, delivery))
                if delay:
                    await asyncio.sleep(delay)
            return results

        semaphore = asyncio.Semaphore(concurrency)

        async def one(delivery: Delivery) -> tuple[str, int, str]:
            async with semaphore:
                result = await deliver(client, base_url, secret, delivery)
                if delay:
                    await asyncio.sleep(delay)
                return result

        results = list(await asyncio.gather(*(one(delivery) for delivery in ordered)))
    return results


# --------------------------------------------------------------------------- #
# state snapshot and comparison
# --------------------------------------------------------------------------- #


def _digest(rows: Iterable[Sequence[Any]]) -> list[str]:
    """Stable, order-independent representation of a set of rows."""
    return sorted("|".join("" if value is None else str(value) for value in row) for row in rows)


def snapshot() -> dict[str, Any]:
    """Summarise the tables a replay touches.

    The digest is intentionally made of *values that must not depend on order*:
    a run's status, a job's status and step count, the set of step names. Adding
    a row or changing a status changes the digest; so does a lost delivery.
    """
    state: dict[str, Any] = {}
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "select github_run_id, run_attempt, status, coalesce(conclusion, '-') "
            "from ct_workflow_runs"
        )
        state["runs"] = _digest(cursor.fetchall())

        cursor.execute("""
            select j.github_job_id, j.status, coalesce(j.conclusion, '-'),
                   count(s.id)
            from ct_jobs j left join ct_steps s on s.job_id = j.id
            group by j.id, j.github_job_id, j.status, j.conclusion
            """)
        state["jobs"] = _digest(cursor.fetchall())

        cursor.execute(
            "select j.github_job_id, s.number, s.status, coalesce(s.conclusion, '-') "
            "from ct_steps s join ct_jobs j on j.id = s.job_id"
        )
        state["steps"] = _digest(cursor.fetchall())

        cursor.execute(
            "select event_type, coalesce(reason, '-'), handled, count(*) "
            "from ct_webhook_delivery group by 1, 2, 3"
        )
        state["ledger"] = _digest(cursor.fetchall())

        cursor.execute("select count(*) from ct_workflows")
        state["workflow_count"] = cursor.fetchone()[0]
    state["digest"] = hashlib.sha256(
        json.dumps({k: v for k, v in state.items() if k != "digest"}, sort_keys=True).encode()
    ).hexdigest()
    return state


def compare(reference: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    """Describe how ``candidate`` differs from ``reference``; empty when equal."""
    problems: list[str] = []
    for key in ("runs", "jobs", "steps", "ledger"):
        expected, actual = set(reference.get(key, [])), set(candidate.get(key, []))
        missing = expected - actual
        unexpected = actual - expected
        if missing:
            problems.append(f"{key}: {len(missing)} present in the reference but missing here")
            for item in sorted(missing)[:5]:
                problems.append(f"    missing: {item}")
        if unexpected:
            problems.append(f"{key}: {len(unexpected)} present here but not in the reference")
            for item in sorted(unexpected)[:5]:
                problems.append(f"    extra:   {item}")
    if reference.get("workflow_count") != candidate.get("workflow_count"):
        problems.append(
            f"workflow_count: {candidate.get('workflow_count')} "
            f"!= {reference.get('workflow_count')}"
        )
    return problems


# --------------------------------------------------------------------------- #
# invariants
# --------------------------------------------------------------------------- #


def check_invariants(capture: Path) -> int:
    """Check the invariants a replay can violate; returns the number of failures."""
    deliveries = load_capture(capture)
    captured_ids = [delivery.delivery_id for delivery in deliveries]
    failures: list[str] = []

    with connect() as connection, connection.cursor() as cursor:
        # 1. every captured delivery is in the ledger and handled
        cursor.execute(
            "select delivery_id, handled, coalesce(reason, '-') from ct_webhook_delivery "
            "where delivery_id = any(%s)",
            (captured_ids,),
        )
        rows = cursor.fetchall()
        seen = {row[0]: row for row in rows}
        missing = [delivery_id for delivery_id in captured_ids if delivery_id not in seen]
        unhandled = [
            (delivery_id, reason)
            for delivery_id, handled, reason in rows
            if not handled and reason not in ("ignored", "unsupported_event")
        ]
        if missing:
            failures.append(
                f"ledger: {len(missing)} captured deliveries were never recorded "
                f"(first: {missing[0]})"
            )
        for delivery_id, reason in unhandled[:10]:
            failures.append(f"ledger: {delivery_id} unhandled ({reason})")

        # 2. no duplicate rows on the three unique keys
        for table, key in (
            ("ct_workflow_runs", "workflow_id, github_run_id"),
            ("ct_jobs", "run_id, github_job_id"),
            ("ct_steps", "job_id, github_step_id"),
        ):
            cursor.execute(
                f"select count(*) from (select {key}, count(*) c from {table} "
                f"group by {key} having count(*) > 1) t"
            )
            duplicates = cursor.fetchone()[0]
            if duplicates:
                failures.append(f"{table}: {duplicates} duplicated key(s) on ({key})")

        # 3. a job the capture described with steps must have them stored
        jobs_with_steps = sorted(
            {
                int(step_payload["workflow_job"]["id"])
                for step_payload in (
                    json.loads(delivery.body)
                    for delivery in deliveries
                    if delivery.event == "workflow_job"
                )
                if step_payload.get("workflow_job", {}).get("steps")
            }
        )
        if jobs_with_steps:
            cursor.execute(
                "select j.github_job_id from ct_jobs j "
                "left join ct_steps s on s.job_id = j.id "
                "where j.github_job_id = any(%s) group by j.github_job_id "
                "having count(s.id) = 0",
                (jobs_with_steps,),
            )
            empty = [row[0] for row in cursor.fetchall()]
            if empty:
                failures.append(
                    f"jobs: {len(empty)} job(s) have no steps although the payload carried "
                    f"them (first: {empty[0]})"
                )

    print(f"capture: {len(deliveries)} deliveries ({len(captured_ids)} unique)")
    if failures:
        print(f"FAIL -- {len(failures)} problem(s):")
        for problem in failures:
            print("  -", problem)
        return len(failures)

    print("PASS -- all invariants hold")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _require_database(allow_production: bool) -> None:
    database = os.environ.get("CITRACKER_DB_NAME", "")
    if not database:
        raise SystemExit("CITRACKER_DB_NAME is not set; point it at the scratch database")
    _refuse_unless_scratch(database, allow_production)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    apply_parser = sub.add_parser("apply", help="replay a capture at the receiver")
    apply_parser.add_argument("capture", type=Path)
    apply_parser.add_argument(
        "--base-url", default=os.environ.get("CITRACKER_REPLAY_URL", "https://127.0.0.1:18080")
    )
    apply_parser.add_argument("--secret", default=os.environ.get("CITRACKER_WEBHOOK_SECRET", ""))
    apply_parser.add_argument("--mode", choices=("sequential", "shuffled"), default="sequential")
    apply_parser.add_argument("--concurrency", type=int, default=1)
    apply_parser.add_argument(
        "--repeat", type=int, default=1, help="replay the capture N times (GitHub redelivery)"
    )
    apply_parser.add_argument("--delay", type=float, default=0.0, help="seconds between requests")
    apply_parser.add_argument("--report", type=Path, help="write per-delivery results as JSON")

    snapshot_parser = sub.add_parser("snapshot", help="write a digest of the replay tables")
    snapshot_parser.add_argument("--output", type=Path, required=True)
    snapshot_parser.add_argument("--allow-production", action="store_true")

    compare_parser = sub.add_parser("compare", help="diff two snapshots")
    compare_parser.add_argument("reference", type=Path)
    compare_parser.add_argument("candidate", type=Path)

    check_parser = sub.add_parser("check", help="check the invariants for a capture")
    check_parser.add_argument("capture", type=Path)
    check_parser.add_argument("--allow-production", action="store_true")

    reset_parser = sub.add_parser(
        "reset", help="truncate the tables a replay writes (scratch database only)"
    )
    reset_parser.add_argument("--allow-production", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "apply":
        if not args.secret:
            raise SystemExit("CITRACKER_WEBHOOK_SECRET (or --secret) is required")
        deliveries = load_capture(args.capture)
        results = asyncio.run(
            apply_capture(
                deliveries,
                base_url=args.base_url,
                secret=args.secret,
                concurrency=args.concurrency,
                repeat=args.repeat,
                shuffle=args.mode == "shuffled",
                delay=args.delay,
            )
        )
        by_status: dict[int, int] = {}
        for _delivery_id, status, _text in results:
            by_status[status] = by_status.get(status, 0) + 1
        print(f"replayed {len(results)} deliveries: {by_status}")
        failures = [(d, s, t) for d, s, t in results if s >= 400]
        for delivery_id, status, text in failures[:10]:
            print(f"  {status} {delivery_id}: {text}")
        if args.report:
            args.report.write_text(
                json.dumps(
                    [{"delivery_id": d, "status": s, "body": t} for d, s, t in results], indent=2
                ),
                encoding="utf-8",
            )
        return 1 if failures else 0

    if args.command == "snapshot":
        _require_database(args.allow_production)
        state = snapshot()
        args.output.write_text(json.dumps(state, indent=2), encoding="utf-8")
        print(f"wrote {args.output} (digest {state['digest'][:16]}…)")
        return 0

    if args.command == "compare":
        reference = json.loads(args.reference.read_text(encoding="utf-8"))
        candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
        problems = compare(reference, candidate)
        if problems:
            print(f"FAIL -- {len(problems)} difference(s) from the sequential reference:")
            for problem in problems:
                print("  -", problem)
            return 1
        print("PASS -- the concurrent replay produced the same state")
        return 0

    if args.command == "check":
        _require_database(args.allow_production)
        return check_invariants(args.capture)

    if args.command == "reset":
        _require_database(args.allow_production)
        with connect() as connection, connection.cursor() as cursor:
            for table in TRUNCATABLE:
                cursor.execute(f"truncate table {table} restart identity cascade")
        print(f"truncated: {', '.join(TRUNCATABLE)}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
