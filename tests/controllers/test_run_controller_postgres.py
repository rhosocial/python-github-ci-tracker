# tests/controllers/test_run_controller_postgres.py
"""The run service against the engine it will actually run on.

the rest of the suite exercises the behaviour on the same PostgreSQL schema,
whose hand-written DDL carries no foreign keys. That is enough to prove
adopt-or-update works, and it is blind to the failure that actually happened: a
job stored before its attempt row exists. SQLite accepts that row; PostgreSQL
refuses it. This module runs the same flow where the constraint bites, and first
asserts the constraint is really there -- otherwise it would be a second SQLite
test with a network call.

The server comes from the same environment contract as the model check: CI sets
variables against its service container, a developer names a scenarios file.
"""

from __future__ import annotations

from conftest import FakeGithub

from rhosocial.github_ci_tracker.controllers import RunData
from rhosocial.github_ci_tracker.controllers.run import RunController
from rhosocial.github_ci_tracker.models import AsyncJob, AsyncRepository, AsyncStep
from rhosocial.github_ci_tracker.models.run import AsyncWorkflowRun
from rhosocial.github_ci_tracker.services.github import GithubRuns

REPO = "acme/demo"
RUN_ID = 500


async def test_a_fetched_run_stores_every_row_inside_the_foreign_keys(
    bound_models,
) -> None:
    """Fetch stores run -> attempt -> jobs -> steps, and the schema enforces it."""
    # Every foreign key the service relies on, in the database it will run on.
    # On SQLite this whole check would pass with no constraints at all.
    constraints = await bound_models.execute(
        "SELECT count(*) AS n FROM information_schema.table_constraints "
        "WHERE table_schema = current_schema() AND constraint_type = 'FOREIGN KEY'"
    )
    assert (constraints.data or [{}])[0].get("n", 0) > 0

    repo = AsyncRepository(
        github_repo_id=4_242,
        full_name="acme/demo",
        owner="acme",
        name="demo",
    )
    await repo.save()

    run = await RunController(runs=GithubRuns(FakeGithub()), runs_data=RunData()).fetch(
        repo, RUN_ID
    )

    assert run.github_run_id == RUN_ID

    # Every row that references another had its parent stored first: the run's
    # attempt, the attempt's job, the job's step.
    assert len(await run.attempt_ids()) == 1
    jobs = await run.jobs_of_attempt()
    assert [job.github_job_id for job in jobs] == [900]
    assert [step.name for step in await jobs[0].steps()] == ["checkout"]
    assert await AsyncJob.query().count() == 1
    assert await AsyncStep.query().count() == 1


async def test_the_derived_state_comes_from_the_newest_attempt(bound_models) -> None:
    """The derived columns are a correlated subquery; PostgreSQL must agree.

    SQLite and PostgreSQL render the same expression, but only one of them is the
    engine this runs on -- and a subquery that is legal here and not there would
    fail on the first production read.
    """
    repo = AsyncRepository(
        github_repo_id=4_243,
        full_name="acme/derived",
        owner="acme",
        name="derived",
    )
    await repo.save()

    service = RunController(runs=GithubRuns(FakeGithub()), runs_data=RunData())
    run = await service.fetch(repo, RUN_ID)

    stored = await AsyncWorkflowRun.find_by_github_id(run.github_run_id, derived=True)

    assert stored is not None
    assert stored.latest_attempt == 1
    assert stored.latest_status == "completed"
    assert stored.latest_conclusion == "success"
    assert stored.latest_completed_at is not None
