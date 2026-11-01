# src/rhosocial/github_ci_tracker/models/__init__.py
"""CI Tracker models: sync/async twins over local persistence.

Every model file declares a sync twin and an `Async`-prefixed async twin with
behavior-identical method pairs; the sync twins exist for the parity rule even
though the running service only uses the async ones. The twins own persistence
and the domain rules that go with it: the token lifecycle, the state machine,
the upsert of a payload, the query scopes.

This package is a leaf. It imports nothing from the rest of the project but
itself -- no services, no client, no credential provider -- so it can be read,
tested and reused on its own. Anything a model would need to import from above
belongs *inside* this package (the state machine, the errors a model raises), or
belongs to the caller (fetching, resolving, talking to GitHub). The rule and its
one-command check are in ``README.md``. See also
`.claude/plan/2026-10-28/controller-owns-github-access.md`.

Relations are declared on the models themselves (``Repository.workflows``,
``Workflow.runs``, ``WorkflowRun.attempts``, ``Job.steps`` and their inverses). The
annotation targets are sibling models in other modules, and every model file
uses ``from __future__ import annotations``, so the descriptor sees a string
that only resolves once the sibling name is in the owning module's globals.
:func:`_publish_relation_targets` does exactly that: it publishes every model
class into every model module, after the classes exist. Doing it here -- the one
module that imports all of them -- keeps the model files free of circular
imports while still letting ``typing.get_type_hints`` resolve the annotations.
"""

from .attempt import AsyncRunAttempt, RunAttempt
from .binding import AsyncRepositoryBinding, RepositoryBinding
from .job import AsyncJob, Job
from .log import AsyncJobLog, AsyncStepLog, JobLog, StepLog
from .pat_credential import AsyncPatCredential, PatCredential
from .repository import AsyncRepository, Repository
from .run import AsyncWorkflowRun, WorkflowRun
from .step import AsyncStep, Step
from .webhook_delivery import (
    EVENT_WORKFLOW_JOB,
    EVENT_WORKFLOW_RUN,
    AsyncWebhookDelivery,
    WebhookDelivery,
)
from .workflow import AsyncWorkflow, Workflow

#: Every model class, in dependency-agnostic order.
_MODEL_CLASSES: tuple[type, ...] = (
    PatCredential,
    RepositoryBinding,
    Repository,
    Workflow,
    WorkflowRun,
    RunAttempt,
    Job,
    Step,
    JobLog,
    StepLog,
    WebhookDelivery,
    AsyncPatCredential,
    AsyncRepositoryBinding,
    AsyncRepository,
    AsyncWorkflow,
    AsyncWorkflowRun,
    AsyncRunAttempt,
    AsyncJob,
    AsyncStep,
    AsyncJobLog,
    AsyncStepLog,
    AsyncWebhookDelivery,
)


def _publish_relation_targets() -> None:
    """Publish every model name into every model module's globals.

    Relation annotations are strings (``from __future__ import annotations``)
    and are resolved through the owning class's module namespace. A relation
    whose target lives in another model module would otherwise raise
    ``NameError`` the first time the descriptor is used, because the target is
    only imported under ``TYPE_CHECKING``.

    Publishing the names here -- after every class exists -- makes the
    annotation resolvable without adding a runtime import cycle to the model
    files. Only names that are model classes are published, so a module's own
    helper functions and constants are untouched.
    """
    import sys

    modules = {sys.modules[cls.__module__]: cls.__module__ for cls in _MODEL_CLASSES}
    for module in modules:
        for cls in _MODEL_CLASSES:
            setattr(module, cls.__name__, cls)


_publish_relation_targets()

#: The async twins, one per entity: exactly the set a process binds to its one
#: connection. Derived, not listed, so a new model cannot be forgotten here.
ASYNC_MODELS: tuple[type, ...] = tuple(
    cls for cls in _MODEL_CLASSES if cls.__name__.startswith("Async")
)


__all__ = [
    "ASYNC_MODELS",
    # sync twins
    "PatCredential",
    "RepositoryBinding",
    "TriggerDecision",
    "Repository",
    "Workflow",
    "WebhookDelivery",
    "WorkflowRun",
    "Job",
    "Step",
    "JobLog",
    "StepLog",
    # async twins
    "AsyncPatCredential",
    "AsyncRepositoryBinding",
    "AsyncRepository",
    "AsyncWorkflow",
    "AsyncWebhookDelivery",
    "AsyncWorkflowRun",
    "AsyncJob",
    "AsyncStep",
    "AsyncJobLog",
    "AsyncStepLog",
    # constants
    "EVENT_WORKFLOW_JOB",
    "EVENT_WORKFLOW_RUN",
]
