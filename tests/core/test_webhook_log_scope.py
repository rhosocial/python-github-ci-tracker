# tests/core/test_webhook_log_scope.py
"""A detached log fetch must not borrow the request's connection.

``asyncio.create_task`` copies the current context, and the request's context
holds the pooled connection the models were bound to. A task started that way
keeps using a connection the response has already returned to the pool -- the
interleaving the ORM warns about for a shared backend, except here it spans two
requests.

So the fetch runs in an empty context and opens a scope of its own. Without a
scope configured it is skipped, which is honest: there is no connection it may
safely use.
"""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import asynccontextmanager
from types import SimpleNamespace

from conftest import make_webhook_service

#: Stands in for "the connection this request is bound to".
REQUEST_CONTEXT = contextvars.ContextVar("request_context", default="none")


class _Scope:
    """The connection scope the application hands the service."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    @asynccontextmanager
    async def open(self):
        self.seen.append(REQUEST_CONTEXT.get())
        yield SimpleNamespace()


class TestLogScope:
    async def test_the_fetch_runs_in_an_empty_context(self, bound) -> None:
        scope = _Scope()
        service = make_webhook_service(log_scope=scope.open)
        token = REQUEST_CONTEXT.set("the request connection")
        try:
            service._schedule_log_fetch(  # noqa: SLF001 - the seam under test
                SimpleNamespace(workflow_id=1, id=1),
                SimpleNamespace(id=1, github_job_id=2),
            )
            await asyncio.gather(*service._log_tasks)  # noqa: SLF001
        finally:
            REQUEST_CONTEXT.reset(token)

        assert scope.seen == ["none"], "the task inherited the caller's connection"

    async def test_without_a_scope_nothing_is_scheduled(self, bound) -> None:
        service = make_webhook_service()

        service._schedule_log_fetch(  # noqa: SLF001 - the seam under test
            SimpleNamespace(workflow_id=1, id=1),
            SimpleNamespace(id=1, github_job_id=2),
        )

        assert service._log_tasks == set()  # noqa: SLF001
