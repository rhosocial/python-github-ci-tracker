# tests/models/test_sync_twins.py
"""The synchronous twins work, and stay twins.

Every entity here has two classes: ``X`` and ``AsyncX``. The service itself only
ever uses the async ones -- the adapters are async and the controllers are async
-- so nothing exercised the sync half, and a class that is declared but never
run drifts silently (a controller method reading a column that does not exist
was found that way).

These tests are the smallest thing that keeps the claim honest:

* a full CI chain written through the sync classes reads back, relation and all;
* every sync class has an ``Async`` twin on the same table, so adding one half
  without the other fails here rather than in a downstream user's code.

The database is the same PostgreSQL schema the async fixtures use, so the two
halves are compared against one schema rather than two.
"""

from __future__ import annotations

import pytest
from rhosocial.activerecord.backend.impl.postgres.backend import PostgresBackend
from rhosocial.activerecord.backend.impl.postgres.config import PostgresConnectionConfig

from rhosocial.github_ci_tracker import models
from rhosocial.github_ci_tracker.models import (
    Job,
    JobLog,
    Repository,
    RunAttempt,
    Step,
    StepLog,
    Workflow,
    WorkflowRun,
)

#: The sync classes this schema binds. The async twins live in the same modules.
SYNC_MODELS = (Repository, Workflow, WorkflowRun, RunAttempt, Job, Step, StepLog, JobLog)


@pytest.fixture()
def sync_postgres(bound, pg_database):
    """The same PostgreSQL schema, opened by every sync twin, torn down after.

    ``bound`` builds the schema from the service's own renderer and binds the
    async twins; this fixture opens a second, synchronous connection to the same
    tables for the sync half. Two connections to one schema is the point: it is
    the arrangement a downstream library user gets.

    One backend for all sync models, not one per class -- the same rule the
    async fixtures follow, and for the same reason.
    """
    backend = PostgresBackend(connection_config=PostgresConnectionConfig(**pg_database))
    backend.connect()
    backend.introspect_and_adapt()
    for model in SYNC_MODELS:
        model.__backend__ = backend
    try:
        yield backend
    finally:
        for model in SYNC_MODELS:
            model.__backend__ = None
        backend.disconnect()


def test_a_whole_chain_round_trips_through_the_sync_twins(sync_postgres) -> None:
    repo = Repository(github_repo_id=1, full_name="acme/demo", owner="acme", name="demo")
    repo.save()
    workflow = Workflow(repository_id=repo.id or 0, workflow_id=7, name="CI", path="ci.yml")
    workflow.save()
    run = WorkflowRun(
        workflow_id=workflow.id or 0,
        github_run_id=11,
        run_number=1,
        event="push",
        branch="main",
        commit_sha="a" * 40,
    )
    run.save()
    attempt = RunAttempt(
        run_id=run.id or 0, attempt_number=1, status="completed", conclusion="failure"
    )
    attempt.save()
    job = Job(
        attempt_id=attempt.id or 0,
        github_job_id=22,
        name="build",
        status="completed",
        conclusion="failure",
    )
    job.save()
    step = Step(
        job_id=job.id or 0,
        github_step_id=1,
        number=1,
        name="Run tests",
        status="completed",
        conclusion="failure",
    )
    step.save()
    StepLog(step_id=step.id or 0, log_content="boom\n").store()
    JobLog(job_id=job.id or 0, log_content="set up\nboom\n").store()

    stored_step = Step.find_one(Step.c.id == (step.id or 0))
    assert stored_step is not None
    assert stored_step.step_log().log_content == "boom\n"
    assert StepLog.find_for_step(step.id or 0).lines() == ["boom"]
    assert JobLog.find_for_job(job.id or 0).lines() == ["set up", "boom"]
    assert Job.find_one(Job.c.id == (job.id or 0)).job_log().log_content.startswith("set up")


def test_every_sync_class_has_an_async_twin_on_the_same_table() -> None:
    """Adding one half without the other is the drift this catches."""
    for sync_class in SYNC_MODELS:
        twin_name = f"Async{sync_class.__name__}"
        twin = getattr(models, twin_name, None)
        assert twin is not None, f"{twin_name} is missing"
        assert (
            twin.__table_name__ == sync_class.__table_name__
        ), f"{twin_name} writes to a different table than {sync_class.__name__}"
