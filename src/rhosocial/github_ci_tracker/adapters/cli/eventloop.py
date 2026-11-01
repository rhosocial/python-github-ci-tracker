# src/rhosocial/github_ci_tracker/adapters/cli/eventloop.py
"""Run one coroutine on a loop this platform's database driver accepts.

psycopg's async connection cannot drive the ProactorEventLoop that Windows uses
by default, so every entry point (the CLIs, the servers) runs its coroutine on a
SelectorEventLoop instead.

``set_event_loop_policy`` is deprecated in 3.14+, and
``asyncio.Runner(loop_factory=...)`` only accepts a factory from 3.12 -- while
this package supports 3.10. Creating, installing and closing the loop by hand
works on every supported version and changes no process-wide policy.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any, TypeVar

_T = TypeVar("_T")


def run_coroutine(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run ``coro`` to completion, on a SelectorEventLoop on Windows."""
    if sys.platform != "win32":
        return asyncio.run(coro)

    loop = asyncio.SelectorEventLoop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


__all__ = ["run_coroutine"]
