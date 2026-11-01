# tests/test_import_order.py
"""Every entry point must import cleanly in a fresh interpreter.

The dependency direction is core -> database, but the ``*-db`` CLI imports
``database`` first, and ``database`` imports ``.core.exceptions`` -- which runs
``core/__init__``. Anything ``core/__init__`` imports is therefore loaded while
``database`` is still half-built, so nothing on that path may reach back into it.
That is also why ``core`` does not re-export the controllers: they import
``core``, so a re-export would close the cycle. In-process tests never see it,
because they import core first; the container's init step does, and died with
``ImportError: cannot import name 'transaction' from partially initialized
module``.

Each module is therefore imported in its own interpreter, in the order that
hurts.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

#: Entry points, plus the two modules on either side of the cycle.
MODULES = [
    "rhosocial.github_ci_tracker.adapters.cli.db",
    "rhosocial.github_ci_tracker.adapters.cli.main",
    "rhosocial.github_ci_tracker.database",
    "rhosocial.github_ci_tracker.services",
    "rhosocial.github_ci_tracker.controllers",
    "rhosocial.github_ci_tracker.views",
    "rhosocial.github_ci_tracker.adapters.rest.app",
    "rhosocial.github_ci_tracker.adapters.mcp.server",
]


@pytest.mark.parametrize("module", MODULES)
def test_a_fresh_interpreter_imports_the_module(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr[-3000:]
