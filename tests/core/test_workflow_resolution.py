# tests/core/test_workflow_resolution.py
"""Resolving a workflow reference: path, file name or id -- never a display name.

A display name lives in the workflow file, so a repository can give one workflow
a different name on every branch: ``vistart/Dockerfiles`` names
``build_and_deploy.yml`` "php - Build and Deploy" on one branch, "vlmcsd - Build
and Deploy" on another. The stored row follows the last run written, so a filter
by name resolves today and not tomorrow. ``gh`` filters runs by branch and by
workflow id or file name for the same reason; a name is something a *run*
reports.

The not-found message therefore lists ``path (id …)`` -- the two spellings that
always resolve -- rather than the name.
"""

from __future__ import annotations

import pytest

from rhosocial.github_ci_tracker.controllers import RepositoryData
from rhosocial.github_ci_tracker.core.exceptions import WorkflowNotFoundError
from rhosocial.github_ci_tracker.models import AsyncRepository, AsyncWorkflow

REPO = "acme/demo"


async def _repo() -> AsyncRepository:
    repo = AsyncRepository(
        github_repo_id=91, full_name=REPO, owner="acme", name="demo", default_branch="main"
    )
    await repo.save()
    await AsyncWorkflow(
        repository_id=repo.id or 0,
        workflow_id=11,
        name="Changelog Check",
        path=".github/workflows/changelog-check.yml",
    ).save()
    await AsyncWorkflow(
        repository_id=repo.id or 0,
        workflow_id=12,
        name="Python Test",
        path=".github/workflows/test.yml",
    ).save()
    return repo


class TestWorkflowResolution:
    async def test_a_display_name_does_not_resolve(self, bound) -> None:
        """A name is per branch, so it cannot identify a workflow."""
        repo = await _repo()

        with pytest.raises(WorkflowNotFoundError) as caught:
            await RepositoryData().workflow(repo, "Changelog Check")

        message = str(caught.value)
        assert ".github/workflows/changelog-check.yml" in message
        assert "id 11" in message

    async def test_a_name_differing_in_case_is_still_not_a_reference(self, bound) -> None:
        """Case-insensitive matching of a free-form string does not make it stable."""
        repo = await _repo()

        with pytest.raises(WorkflowNotFoundError):
            await RepositoryData().workflow(repo, "changelog check")

    async def test_a_path_and_its_file_name_both_resolve(self, bound) -> None:
        """The two spellings a caller can always derive: the path and its last part."""
        repo = await _repo()

        by_path = await RepositoryData().workflow(repo, ".github/workflows/test.yml")
        by_file_name = await RepositoryData().workflow(repo, "test.yml")

        assert by_path.workflow_id == by_file_name.workflow_id == 12

    async def test_the_numeric_id_resolves_regardless_of_the_name(self, bound) -> None:
        repo = await _repo()

        workflow = await RepositoryData().workflow(repo, "11")

        assert workflow.path == ".github/workflows/changelog-check.yml"

    async def test_the_file_name_still_resolves(self, bound) -> None:
        repo = await _repo()

        workflow = await RepositoryData().workflow(repo, "test.yml")

        assert workflow.workflow_id == 12

    async def test_an_unknown_name_lists_the_available_workflows(self, bound) -> None:
        repo = await _repo()

        with pytest.raises(WorkflowNotFoundError) as caught:
            await RepositoryData().workflow(repo, "Changelog")

        message = str(caught.value)
        # Paths and ids: the name is the part that can differ per branch.
        assert ".github/workflows/changelog-check.yml (id 11)" in message
        assert ".github/workflows/test.yml (id 12)" in message
        # The internal ORM class must not leak into a message a model reads.
        assert "AsyncWorkflow" not in message
        assert "AsyncRepository" not in message

    async def test_a_renamed_workflow_still_resolves_by_path_and_id(self, bound) -> None:
        """The name follows whichever branch ran last; the path and the id do not.

        GitHub scopes a workflow to its file path, so two branches can give the
        same workflow different display names -- and storing a run from the other
        branch rewrites the name. That is why a reference has to be the path or
        the id: the label is not an identity.
        """
        repo = await _repo()
        stored = await RepositoryData().workflow(repo, ".github/workflows/test.yml")
        stored.name = "php - Python Test"
        await stored.save()

        by_path = await RepositoryData().workflow(repo, ".github/workflows/test.yml")
        by_file_name = await RepositoryData().workflow(repo, "test.yml")
        by_id = await RepositoryData().workflow(repo, "12")

        assert by_path.workflow_id == by_file_name.workflow_id == by_id.workflow_id == 12
        with pytest.raises(WorkflowNotFoundError):
            await RepositoryData().workflow(repo, "Python Test")

    async def test_a_repository_without_stored_workflows_says_how_to_load_them(self, bound) -> None:
        repo = AsyncRepository(
            github_repo_id=92, full_name="acme/empty", owner="acme", name="empty"
        )
        await repo.save()

        with pytest.raises(WorkflowNotFoundError) as caught:
            await RepositoryData().workflow(repo, "ci.yml")

        assert "list_repos" in str(caught.value)
