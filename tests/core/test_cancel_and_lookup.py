# tests/core/test_cancel_and_lookup.py
"""Resolving a run's repository from its id.

A run id is globally unique, so a caller holding one should not have to remember
which repository it belongs to. The cancel path itself lives in
``tests/controllers/test_dispatch_controller.py`` now; what remains here is the lookup
the MCP adapter offers before it can answer anything repository-scoped.
"""

from __future__ import annotations

import pytest
from conftest import FakeGithub  # noqa: F401 - the fixture of the same name

from rhosocial.github_ci_tracker.core.exceptions import RunNotFoundError
from rhosocial.github_ci_tracker.models import (
    AsyncRepository,
    AsyncWorkflow,
    AsyncWorkflowRun,
)

REPO = "acme/demo"
RUN_ID = 8123


async def _ready_repo() -> AsyncRepository:
    repo = AsyncRepository(
        github_repo_id=77, full_name=REPO, owner="acme", name="demo", default_branch="main"
    )
    await repo.save()
    return repo


async def _run(repo: AsyncRepository) -> AsyncWorkflowRun:
    workflow = AsyncWorkflow(
        repository_id=repo.id or 0,
        workflow_id=7,
        name="CI",
        path=".github/workflows/test.yml",
    )
    await workflow.save()
    run = AsyncWorkflowRun(
        workflow_id=workflow.id or 0,
        github_run_id=RUN_ID,
        run_number=1,
        run_attempt=1,
        status="in_progress",
        conclusion=None,
        branch="main",
        commit_sha="c" * 40,
        event="push",
    )
    await run.save()
    return run


class TestRepositoryLookup:
    async def test_a_tracked_run_resolves_its_repository(self, bound) -> None:
        from rhosocial.github_ci_tracker.adapters.mcp.tools.query import _repository_of_run
        from rhosocial.github_ci_tracker.container import Container

        repo = await _ready_repo()
        await _run(repo)

        assert await _repository_of_run(Container.build(), RUN_ID) == REPO

    async def test_an_untracked_run_says_what_to_do(self, bound) -> None:
        from rhosocial.github_ci_tracker.adapters.mcp.tools.query import _repository_of_run
        from rhosocial.github_ci_tracker.container import Container

        await _ready_repo()

        with pytest.raises(RunNotFoundError) as excinfo:
            await _repository_of_run(Container.build(), 424242)
        assert "find_runs" in str(excinfo.value)
