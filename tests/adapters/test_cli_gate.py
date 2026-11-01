# tests/adapters/test_cli_gate.py
"""The CLI is authorised by database access, not by CITRACKER_API_KEY.

Only the protocol surfaces (REST, MCP) use the key. These tests pin the two
commands that were previously blocked by the global gate even though neither
reads it: ``credential`` and ``serve webhook``.
"""

from __future__ import annotations

import pytest

from rhosocial.github_ci_tracker.adapters.cli import main as cli


def test_serve_webhook_runs_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The webhook receiver authenticates GitHub, not the operator."""
    monkeypatch.delenv("CITRACKER_API_KEY", raising=False)
    seen: dict[str, str] = {}

    def fake_serve(args: object, api_key: str) -> None:
        seen["service"] = args.service  # type: ignore[attr-defined]
        seen["key"] = api_key

    monkeypatch.setattr(cli.serve, "run", fake_serve)

    cli.main(["serve", "webhook"])

    assert seen == {"service": "webhook", "key": ""}


def test_credential_runs_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The credential command manages the database; the key is not its input."""
    monkeypatch.delenv("CITRACKER_API_KEY", raising=False)
    called: list[str] = []

    def fake_run_with_db(runner: object, args: object) -> None:
        called.append("dispatched")

    monkeypatch.setattr(cli, "_run_with_db", fake_run_with_db)

    cli.main(["credential", "pat", "list"])

    assert called == ["dispatched"]
