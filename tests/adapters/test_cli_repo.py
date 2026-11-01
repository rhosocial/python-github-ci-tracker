# tests/adapters/test_cli_repo.py
"""The ``repo`` verbs must reach their handler, not die on an import.

``run`` imports ``RepositoryNotFoundError`` *before* dispatching, and a wrong
relative depth there (``...core`` from ``adapters/cli/commands`` resolves to
``adapters.core``) is invisible to every test that only imports the module or
parses arguments: the failure needs one command to actually run. It took the
whole family down -- ``subscribe``, ``unsubscribe``, ``bind``, ``unbind``,
``list``, ``discover`` -- and was found by running ``repo subscribe`` against a
deployment, which is exactly where nothing catches it.

The import now sits one level too shallow to be believable (`....core`), so this
test runs a verb end to end and asserts it reaches the "no such subscription"
path instead of raising ``ModuleNotFoundError``.
"""

from __future__ import annotations

import argparse
import asyncio
from types import SimpleNamespace

import pytest

from rhosocial.github_ci_tracker.adapters.cli.commands import repo as repo_command
from rhosocial.github_ci_tracker.core.exceptions import RepositoryNotFoundError


class _Repositories:
    """A repository service that answers "not subscribed" for every verb."""

    async def list_repositories(self):  # noqa: ANN202
        raise RepositoryNotFoundError("repository 'acme/demo' is not tracked")


def _args(verb: str) -> argparse.Namespace:
    return argparse.Namespace(
        repo_command=verb,
        name="acme/demo",
        credential=None,
        format="json",
        verbose=False,
    )


def test_a_verb_reaches_its_handler_and_reports_an_unknown_repository(capsys) -> None:
    container = SimpleNamespace(repository=_Repositories())

    with pytest.raises(SystemExit) as exit_info:
        asyncio.run(repo_command.run(_args("list"), container))

    assert exit_info.value.code == 1
    stderr = capsys.readouterr().err
    assert "not tracked" in stderr
    assert "repo subscribe --name acme/demo" in stderr


def test_an_unknown_verb_is_a_usage_error() -> None:
    container = SimpleNamespace(repository=_Repositories())

    with pytest.raises(SystemExit) as exit_info:
        asyncio.run(repo_command.run(_args("nonexistent"), container))

    assert exit_info.value.code == 2
