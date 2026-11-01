# tests/models/test_standalone.py
"""`models` must work on its own.

Three checks, weakest to strongest:

1. **Static** -- no module under ``models/`` imports any other part of the
   project. The rule lives in ``models/README.md``; this is the executable form
   of it, so a regression fails a test instead of needing someone to remember
   the ``rg`` command.
2. **Import** -- a fresh interpreter that imports ``models`` loads nothing else.
   The static check cannot see a dependency that arrives through a helper,
   a plugin, or a conditional import; this one observes what actually loaded.
3. **Runtime** -- with a real PostgreSQL backend and nothing but the ORM, the
   models create, read, relate and delete their own rows. If the layer needed a
   service, a client or a credential provider to work, this would fail.

The first two need no database. The third needs a PostgreSQL server, and takes
its address from the environment: ``CITRACKER_DB_*`` (the variables the
application itself reads), falling back to the libpq ``PG*`` names, merged over a
scenario file when one is named or present. Nothing here knows where the server
is; CI only sets variables. Locally, point ``CITRACKER_TEST_PG_SCENARIO_FILE`` at
a scenarios file -- the sibling ``python-activerecord-postgres`` repository ships
one with a ``postgres_18`` entry -- or just set ``CITRACKER_DB_HOST``.

The runtime check works inside a private schema and drops it on the way out, so
it neither depends on nor disturbs whatever else lives on that server.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from rhosocial.github_ci_tracker import models

#: The package under test, for the static scan.
MODELS_DIR = Path(models.__file__).parent

TEST_SCHEMA = "citracker_models_standalone"


# ---------------------------------------------------------------------------
# 1. static: nothing leaves the package
# ---------------------------------------------------------------------------


def _outbound_imports() -> list[str]:
    """Every import under ``models/`` that points outside the package."""
    found: list[str] = []
    for path in sorted(MODELS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level >= 2 and not module.startswith("models"):
                    found.append(f"{path.name}:{node.lineno} from {'.' * node.level}{module}")
                elif node.level == 0 and module.startswith("rhosocial.github_ci_tracker"):
                    if not module.startswith("rhosocial.github_ci_tracker.models"):
                        found.append(f"{path.name}:{node.lineno} from {module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    if name.startswith("rhosocial.github_ci_tracker") and not name.startswith(
                        "rhosocial.github_ci_tracker.models"
                    ):
                        found.append(f"{path.name}:{node.lineno} import {name}")
    return found


def test_models_imports_nothing_from_the_rest_of_the_project() -> None:
    """The leaf rule, as an assertion instead of a documented command."""
    outbound = _outbound_imports()
    assert (
        outbound == []
    ), "models may import only itself and the ORM; these point elsewhere:\n  " + "\n  ".join(
        outbound
    )


# ---------------------------------------------------------------------------
# 2. import: a fresh interpreter loads nothing else
# ---------------------------------------------------------------------------

_PROBE = """
import sys
import rhosocial.github_ci_tracker.models  # noqa: F401

prefix = "rhosocial.github_ci_tracker."
leaked = sorted(
    name
    for name in sys.modules
    if name.startswith(prefix) and not name.startswith(prefix + "models")
)
print("\\n".join(leaked))
"""


def test_a_fresh_interpreter_loads_only_models() -> None:
    """Importing ``models`` must not pull in another layer, directly or not."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-3000:]

    leaked = [line for line in result.stdout.splitlines() if line.strip()]
    assert leaked == [], "importing models also loaded:\n  " + "\n  ".join(leaked)


# ---------------------------------------------------------------------------
# 3. runtime: a real PostgreSQL, and nothing above the models
# ---------------------------------------------------------------------------


async def test_models_crud_on_their_own(bound_models) -> None:
    """The whole entity chain, with the ORM as the only collaborator."""
    repo = models.AsyncRepository(
        github_repo_id=4_242,
        full_name="acme/demo",
        owner="acme",
        name="demo",
    )
    await repo.save()
    assert repo.id is not None

    workflow = models.AsyncWorkflow(
        repository_id=repo.id,
        workflow_id=7_001,
        name="CI",
        path=".github/workflows/ci.yml",
        state="active",
    )
    await workflow.save()

    run = models.AsyncWorkflowRun(
        workflow_id=workflow.id or 0,
        github_run_id=90_001,
        run_number=12,
        run_attempt=1,
        status="queued",
        branch="main",
    )
    await run.save()
    attempt = await models.AsyncRunAttempt.ensure(run, 1)

    job = models.AsyncJob(
        attempt_id=attempt.id or 0,
        github_job_id=80_001,
        name="tests",
        status="in_progress",
    )
    await job.save()
    await models.AsyncStep(
        job_id=job.id or 0,
        github_step_id=70_001,
        number=1,
        name="pytest",
        status="in_progress",
    ).save()

    # Read back through each relation the models declare.
    stored = await models.AsyncRepository.find_one(
        models.AsyncRepository.c.full_name == "acme/demo"
    )
    assert stored is not None
    assert stored.github_repo_id == 4_242

    workflows = await stored.workflows()
    assert [row.path for row in workflows] == [".github/workflows/ci.yml"]

    runs = await workflow.runs()
    assert [row.github_run_id for row in runs] == [90_001]

    jobs = await run.jobs_of_attempt()
    assert [job.github_job_id for job in jobs] == [80_001]

    steps = await jobs[0].steps()
    assert [step.name for step in steps] == ["pytest"]

    # Update, then delete bottom-up: no service ever saw these rows.
    stored.default_branch = "develop"
    await stored.save()
    again = await models.AsyncRepository.find_one(models.AsyncRepository.c.github_repo_id == 4_242)
    assert again is not None
    assert again.default_branch == "develop"

    await steps[0].delete()
    await jobs[0].delete()
    await attempt.delete()
    await run.delete()
    await workflow.delete()
    await stored.delete()

    assert (
        await models.AsyncRepository.find_one(models.AsyncRepository.c.github_repo_id == 4_242)
        is None
    )
