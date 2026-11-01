# src/rhosocial/github_ci_tracker/models/run.py
"""Workflow run model, sync and async twins.

A run is one GitHub Actions workflow execution, mirrored locally and updated
exclusively through the two passive channels: a GitHub webhook delivery or an
explicit pull. The pull channel never short-circuits on a terminal local state,
so it doubles as the reconciliation tool for a missed webhook, and it adopts runs
the system has never seen (:meth:`WorkflowRun.adopt_from_remote`, which upserts
the run's workflow first).

Neither channel is implemented here: reading a run from GitHub and deciding what
to do with it is service work (:meth:`QueryController.refresh_run
<rhosocial.github_ci_tracker.controllers.query.QueryController.refresh_run>`, and
:meth:`TriggerController.wait_for_completion
<rhosocial.github_ci_tracker.controllers.trigger.TriggerController.wait_for_completion>`
for
polling). The model takes payloads it is handed -- ``adopt_from_remote``,
``update_from_remote`` and ``sync_jobs_from_remote`` -- and owns the write rules
they imply: the attempt-scoped state machine, the attempt row every job hangs
off, and the job/step cascade.

The query domain deliberately exercises the three query builders of the ORM:
plain ``ActiveQuery`` chaining (:meth:`WorkflowRun.recent`,
:meth:`WorkflowRun.latest_by_branch`), a ``SetOperationQuery`` UNION ALL of
per-workflow top-k subqueries (:meth:`WorkflowRun.latest_per_workflow`) and a
``CTEQuery`` aggregate (:meth:`WorkflowRun.status_summary`).

The twins are behavior-identical; the sync one exists for the project's
sync/async parity rule. References that would make the import graph cyclic --
the repository, workflow, job, step models and the ``core`` package (whose
``__init__`` imports the event handlers, which import the models) -- are
imported lazily inside method bodies; the type annotations use
``TYPE_CHECKING`` imports instead.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from rhosocial.activerecord.backend.errors import IntegrityError
from rhosocial.activerecord.backend.expression.core import Subquery
from rhosocial.activerecord.base.field_proxy import FieldProxy
from rhosocial.activerecord.base.fields import DerivedField
from rhosocial.activerecord.field.timestamp import DefaultTimestampMixin
from rhosocial.activerecord.model import ActiveRecord, AsyncActiveRecord
from rhosocial.activerecord.query import ActiveQuery, AsyncActiveQuery
from rhosocial.activerecord.relation.async_descriptors import AsyncBelongsTo
from rhosocial.activerecord.relation.descriptors import BelongsTo

from .relations import AsyncOrderedHasMany, OrderedHasMany

if TYPE_CHECKING:
    from .attempt import AsyncRunAttempt, RunAttempt
    from .job import AsyncJob, Job
    from .repository import AsyncRepository, Repository
    from .workflow import AsyncWorkflow, Workflow

logger = logging.getLogger(__name__)


def _as_int(value: Any) -> int | None:
    """Coerce a GitHub scalar to ``int``, returning ``None`` when impossible."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_str(value: Any, default: str = "") -> str:
    """Return a trimmed string, falling back to ``default`` for blank values."""
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _head_commit(run_data: Mapping[str, Any]) -> Mapping[str, Any]:
    """The ``head_commit`` object of a run payload, or an empty mapping."""
    head_commit = run_data.get("head_commit")
    return head_commit if isinstance(head_commit, Mapping) else {}


def _commit_message(run_data: Mapping[str, Any]) -> str:
    """Extract the commit message from a REST run or webhook payload."""
    return _clean_str(_head_commit(run_data).get("message"))


def _commit_time(run_data: Mapping[str, Any]) -> datetime | None:
    """The commit's own timestamp, when the payload carries one."""
    from .states import parse_timestamp

    return parse_timestamp(_head_commit(run_data).get("timestamp"))


def _commit_author(run_data: Mapping[str, Any]) -> str:
    """The commit author's name.

    Distinct from the run's ``actor``: one is who wrote the code, the other is
    who asked CI to run it, and a re-run makes them different people.
    """
    author = _head_commit(run_data).get("author")
    if isinstance(author, Mapping):
        return _clean_str(author.get("name"))
    return ""


def _head_repo(run_data: Mapping[str, Any]) -> str:
    """``head_repository.full_name``, which differs from the subscription on a fork."""
    head_repository = run_data.get("head_repository")
    if isinstance(head_repository, Mapping):
        return _clean_str(head_repository.get("full_name"))
    return ""


def _base_ref(run_data: Mapping[str, Any]) -> str:
    """Target branch of the pull request that produced the run, if any."""
    pull_requests = run_data.get("pull_requests")
    if not isinstance(pull_requests, list) or not pull_requests:
        return ""
    first = pull_requests[0]
    if not isinstance(first, Mapping):
        return ""
    base = first.get("base")
    return _clean_str(base.get("ref")) if isinstance(base, Mapping) else ""


def _display_title(run_data: Mapping[str, Any]) -> str:
    """GitHub's own one-line title for the run."""
    return _clean_str(run_data.get("display_title"))


def _actor(run_data: Mapping[str, Any]) -> str:
    """Extract the triggering actor login from a run payload."""
    for key in ("actor", "triggering_actor"):
        actor = run_data.get(key)
        if isinstance(actor, Mapping):
            login = _clean_str(actor.get("login"))
            if login:
                return login
        elif actor:
            return _clean_str(actor)
    return ""


def _latest_column(dialect: Any, model_name: str, attribute: str) -> Any:
    """One column of a run's newest attempt, resolved once the class exists.

    The derived-field factories are re-created by ``typing.get_type_hints`` while
    the model class is still being defined, and the globals they capture are a
    snapshot from that moment -- so they must not name the class directly. The
    lookup happens here, at query time, through the live module.
    """
    model = getattr(sys.modules[__name__], model_name)
    return model._latest_column(dialect, attribute)


def _belongs_to_pull_request(data: Mapping[str, Any], number: int, head_sha: str) -> bool:
    """Whether a remote run payload belongs to a pull request.

    GitHub offers no PR-number filter on the runs endpoint, so attribution is
    done here in two tiers:

    * When the run carries a non-empty ``pull_requests`` array, it is
      authoritative and exclusive -- GitHub populates it while the pull request
      is open (the state this feature targets), so a number miss means the run
      belongs to a different pull request.
    * When the array is empty or absent (closed/merged pull requests, where
      GitHub drops the attribution), the run is matched on ``event`` plus a
      matching ``head_sha``. Best-effort: two pull requests sharing a head commit
      become indistinguishable, which is why open pull requests are the case
      this supports.
    """
    prs = data.get("pull_requests")
    if prs:
        return any(isinstance(pr, Mapping) and _as_int(pr.get("number")) == number for pr in prs)
    event = _clean_str(data.get("event"))
    sha = _clean_str(data.get("head_sha"))
    return event == "pull_request" and bool(head_sha) and sha == head_sha


def _pull_request_number(data: Mapping[str, Any]) -> int | None:
    """Cached PR number from a run payload, or ``None``.

    Only the first entry is used: a run belongs to one pull request in practice,
    and the field is absent for fork PRs, so ``None`` is "unknown", not "none".
    """
    pull_requests = data.get("pull_requests")
    if not pull_requests:
        return None
    for entry in pull_requests:
        if isinstance(entry, Mapping):
            number = _as_int(entry.get("number"))
            if number:
                return number
    return None


def _run_url(repo_full_name: str, github_run_id: int) -> str:
    """Build the GitHub HTML URL of a run."""
    return f"https://github.com/{repo_full_name}/actions/runs/{github_run_id}"


def _elapsed_seconds(
    started_at: datetime | None,
    completed_at: datetime | None,
) -> float | None:
    """Elapsed seconds between two timestamps (``None`` without a start)."""
    if started_at is None:
        return None
    end = completed_at or datetime.now(started_at.tzinfo)
    return (end - started_at).total_seconds()


class _RunStateFields:
    """The derived fields of the synchronous run, declared where they resolve.

    ``DerivedField`` is discovered by ``typing.get_type_hints`` over each class
    in the MRO. The model's own annotations name related models (``RunAttempt``,
    ``Job``) that are imported under ``TYPE_CHECKING`` only, so resolving *its*
    hints fails and derived fields declared there would be invisible. This mixin
    names nothing model-specific, so it resolves cleanly.

    The factories are assigned in the class body, which runs with the module's
    globals; the class each one looks up is resolved at query time.
    """

    latest_attempt: ClassVar[DerivedField] = DerivedField(
        lambda d: _latest_column(d, "WorkflowRun", "attempt_number")
    )
    latest_status: ClassVar[DerivedField] = DerivedField(
        lambda d: _latest_column(d, "WorkflowRun", "status")
    )
    latest_conclusion: ClassVar[DerivedField] = DerivedField(
        lambda d: _latest_column(d, "WorkflowRun", "conclusion")
    )
    latest_started_at: ClassVar[DerivedField] = DerivedField(
        lambda d: _latest_column(d, "WorkflowRun", "started_at")
    )
    latest_github_created_at: ClassVar[DerivedField] = DerivedField(
        lambda d: _latest_column(d, "WorkflowRun", "github_created_at")
    )
    latest_completed_at: ClassVar[DerivedField] = DerivedField(
        lambda d: _latest_column(d, "WorkflowRun", "completed_at")
    )


class _AsyncRunStateFields:
    """The derived fields of the asynchronous run; see :class:`_RunStateFields`."""

    latest_attempt: ClassVar[DerivedField] = DerivedField(
        lambda d: _latest_column(d, "AsyncWorkflowRun", "attempt_number")
    )
    latest_status: ClassVar[DerivedField] = DerivedField(
        lambda d: _latest_column(d, "AsyncWorkflowRun", "status")
    )
    latest_conclusion: ClassVar[DerivedField] = DerivedField(
        lambda d: _latest_column(d, "AsyncWorkflowRun", "conclusion")
    )
    latest_started_at: ClassVar[DerivedField] = DerivedField(
        lambda d: _latest_column(d, "AsyncWorkflowRun", "started_at")
    )
    latest_github_created_at: ClassVar[DerivedField] = DerivedField(
        lambda d: _latest_column(d, "AsyncWorkflowRun", "github_created_at")
    )
    latest_completed_at: ClassVar[DerivedField] = DerivedField(
        lambda d: _latest_column(d, "AsyncWorkflowRun", "completed_at")
    )


class WorkflowRun(_RunStateFields, DefaultTimestampMixin, ActiveRecord):
    """A single workflow run, synchronous twin."""

    __table_name__ = "ct_workflow_runs"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    workflow_id: int = 0
    github_run_id: int = 0
    run_number: int = 0
    #: Cached pull request attribution. ``None`` never means "no PR": GitHub
    #: leaves ``pull_requests[]`` empty on fork PRs, so this is an accelerator
    #: only. Queries by PR resolve the head sha instead.
    pr_number: int | None = None
    event: str = ""
    branch: str = ""
    commit_sha: str = ""
    commit_message: str = ""
    #: GitHub's own one-line title for this run (commit subject, or ``title #n``).
    display_title: str = ""
    #: The repository the code came from; differs from the subscription on a fork.
    head_repo: str = ""
    #: The commit's timestamp, which is not when CI started.
    commit_time: datetime | None = None
    #: The commit author, which is not who triggered the run.
    commit_author: str = ""
    #: Target branch of the pull request, when the run has one.
    base_ref: str = ""
    actor: str = ""
    #: Last webhook delivery that touched this row.
    webhook_received_at: datetime | None = None

    #: Owning workflow (pairs with :attr:`Workflow.runs`).
    workflow: ClassVar[BelongsTo[Workflow]] = BelongsTo(
        foreign_key="workflow_id", inverse_of="runs"
    )
    #: Every stored execution of this run, in attempt-number order. A job no
    #: longer hangs off the run directly (``attempt_id -> run_id`` would be a
    #: BCNF violation), so reading the run's jobs goes through these.
    attempts: ClassVar[OrderedHasMany[RunAttempt]] = OrderedHasMany(
        foreign_key="run_id",
        inverse_of="run",
        order_key=lambda attempt: attempt.attempt_number,
    )

    # ---- derived: the state of the newest attempt ---------------------------
    #
    # A run row carries identity and metadata only. Its status, conclusion and
    # timestamps are facts about one *execution*, and those live in
    # ``ct_run_attempts``. The fields below are computed by the database at
    # query time, so "the run's current state" and "its newest attempt's state"
    # cannot disagree: there is only one copy of the fact.
    #
    # They are loaded by ``find_one`` / ``find_all`` with ``derived=True``; a
    # query that does not ask for them leaves them ``None``.

    @classmethod
    def _latest_column(cls, dialect: Any, attribute: str) -> Any:
        """Correlated scalar subquery: one column of this run's newest attempt.

        ``ORDER BY attempt_number DESC LIMIT 1`` uses the
        ``(run_id, attempt_number)`` unique key, so each derived column is one
        index lookup per row.
        """
        column = getattr(RunAttempt.c, attribute)
        inner = (
            RunAttempt.query()
            .select(column)
            .where(RunAttempt.c.run_id == cls.c.id)
            .order_by((RunAttempt.c.attempt_number, "DESC"))
            .limit(1)
        )
        return Subquery(dialect, inner.to_sql())

    def _apply_attempt_state(self, attempt: RunAttempt | None) -> None:
        """Expose an attempt's state through the derived fields, in memory.

        A write path already has the attempt row in hand; re-reading the run
        through a ``derived`` query just to fill these attributes would
        cost one query per delivery. ``None`` leaves them unset.
        """
        if attempt is None:
            return
        self.__dict__["latest_attempt"] = attempt.attempt_number
        self.__dict__["latest_status"] = attempt.status
        self.__dict__["latest_conclusion"] = attempt.conclusion
        self.__dict__["latest_started_at"] = attempt.started_at
        self.__dict__["latest_completed_at"] = attempt.completed_at
        self.__dict__["latest_github_created_at"] = attempt.github_created_at

    # ---- query domain -------------------------------------------------------

    @classmethod
    def find_by_github_id(cls, github_run_id: int, *, derived: bool = False) -> WorkflowRun | None:
        """Return the stored record of a GitHub run id, or ``None``.

        ``derived`` loads ``latest_status`` and friends -- the state that lives
        on the newest attempt -- in the same query.
        """
        return cls.find_one(cls.c.github_run_id == github_run_id, derived=derived)

    @classmethod
    def recent(
        cls,
        workflow_ids: list[int],
        branch: str | None = None,
    ) -> ActiveQuery:
        """Query builder over the runs of ``workflow_ids``, newest first.

        There is no ``status`` filter: status belongs to an attempt, not to a
        run, so filtering by it would need the latest-attempt subquery in the
        ``WHERE`` clause -- a query the callers that need it can build from
        :meth:`RunAttempt.latest_for_run`.

        Args:
            workflow_ids: Local workflow ids to scope to; the caller must
                pass a non-empty list.
            branch: Optional branch filter.

        Returns:
            An :class:`ActiveQuery` ordered by ``run_number`` descending.
        """
        query = cls.query().where(cls.c.workflow_id.in_(workflow_ids))
        if branch:
            query = query.where(cls.c.branch == branch)
        return query.order_by((cls.c.run_number, "DESC"))

    @classmethod
    def latest_by_branch(cls, workflow_ids: list[int], branch: str) -> WorkflowRun | None:
        """Return the newest run of ``workflow_ids`` on ``branch``, or ``None``."""
        if not workflow_ids:
            return None
        return cls.recent(workflow_ids, branch=branch).one()

    @classmethod
    def latest_per_workflow(cls, workflow_ids: list[int], k: int = 1) -> list[WorkflowRun]:
        """Return the ``k`` newest runs of every workflow in ``workflow_ids``.

        Fetches all matching runs in a single query (ordered by ``run_number``
        DESC) and keeps the first *k* per workflow in Python. This avoids the
        ``SetOperationQuery`` rendering defect where UNION ALL branches with
        ``ORDER BY ... LIMIT`` produce invalid SQL.

        Args:
            workflow_ids: Local workflow ids to scope to.
            k: How many runs to keep per workflow (at least one).

        Returns:
            The matched runs (empty when ``workflow_ids`` is empty).
        """
        if not workflow_ids:
            return []
        rows = cls._build_latest_per_workflow_query(workflow_ids, k).all()
        counts: dict[int, int] = {}
        result: list[WorkflowRun] = []
        for row in rows:
            wid = row.workflow_id
            counts[wid] = counts.get(wid, 0) + 1
            if counts[wid] <= k:
                result.append(row)
        return result

    @classmethod
    def _build_latest_per_workflow_query(cls, workflow_ids: list[int], k: int) -> Any:
        """Build a query returning the latest *k* runs per workflow.

        The library's ``SetOperationQuery`` renders UNION ALL operands without
        parentheses, so branches containing ``ORDER BY ... LIMIT`` produce
        invalid SQL (documented in python-activerecord plan
        ``2026-10-11/set-operation-query-rendering.md``). This method works
        around that by fetching all matching runs in one query and deduplicating
        per workflow in Python.
        """
        return (
            cls.query()
            .where(cls.c.workflow_id.in_(workflow_ids))
            .order_by((cls.c.run_number, "DESC"))
        )

    @classmethod
    def status_summary(cls, workflow_ids: list[int]) -> list[dict[str, Any]]:
        """Aggregate run counts per workflow, by each run's newest attempt.

        Status is not a run column: it belongs to the execution, so the counts
        are taken from the newest attempt of every run. Two queries -- the runs
        of those workflows, then their attempts -- and the newest attempt per
        run is kept in Python.

        Args:
            workflow_ids: Local workflow ids to scope to.

        Returns:
            One dictionary per workflow with ``workflow_id``, ``total``,
            ``completed``, ``success``, ``failure`` and ``in_progress``
            (workflows with no runs are absent).
        """
        if not workflow_ids:
            return []

        from .attempt import RunAttempt

        runs = (
            cls.query()
            .select(cls.c.id, cls.c.workflow_id)
            .where(cls.c.workflow_id.in_(workflow_ids))
            .all()
        )
        by_run: dict[int, int] = {row.id: row.workflow_id for row in runs if row.id is not None}
        if not by_run:
            return []

        attempts = (
            RunAttempt.query()
            .select(
                RunAttempt.c.run_id,
                RunAttempt.c.attempt_number,
                RunAttempt.c.status,
                RunAttempt.c.conclusion,
            )
            .where(RunAttempt.c.run_id.in_(list(by_run)))
            .order_by((RunAttempt.c.run_id, "ASC"))
            .order_by((RunAttempt.c.attempt_number, "DESC"))
            .all()
        )
        newest: dict[int, tuple[str, str | None]] = {}
        for row in attempts:
            newest.setdefault(row.run_id, (row.status, row.conclusion))

        counts: dict[int, dict[str, Any]] = {}
        for run_id, workflow_id in by_run.items():
            entry = counts.setdefault(
                workflow_id,
                {
                    "workflow_id": workflow_id,
                    "total": 0,
                    "completed": 0,
                    "success": 0,
                    "failure": 0,
                    "in_progress": 0,
                },
            )
            entry["total"] += 1
            status, conclusion = newest.get(run_id, ("queued", None))
            if status == "completed":
                entry["completed"] += 1
                if conclusion == "success":
                    entry["success"] += 1
                elif conclusion == "failure":
                    entry["failure"] += 1
            elif status == "in_progress":
                entry["in_progress"] += 1
        return list(counts.values())

    # ---- behavior -------------------------------------------------------------

    @classmethod
    def adopt_from_remote(cls, repo: Repository, remote: Mapping[str, Any]) -> WorkflowRun:
        """Store a run that only exists on GitHub so far.

        The run's workflow is upserted first (:meth:`Workflow.upsert_from_remote`),
        so the pull channel can adopt a run of a workflow the system has
        never tracked. The start timestamp accepts both GitHub spellings
        (``run_started_at`` on REST payloads, ``started_at`` on webhooks).

        Args:
            repo: The subscription the run belongs to.
            remote: A GitHub run payload (REST object or webhook fragment).

        Returns:
            The persisted :class:`WorkflowRun` record.
        """
        from .workflow import Workflow

        workflow = Workflow(
            repository_id=repo.id or 0,
            workflow_id=_as_int(remote.get("workflow_id")) or 0,
            name=_clean_str(remote.get("name")),
            path=_clean_str(remote.get("path")),
            state="active",
        )
        workflow = workflow.upsert_from_remote(remote)

        run = cls(
            workflow_id=workflow.id or 0,
            github_run_id=_as_int(remote.get("id")) or 0,
            run_number=_as_int(remote.get("run_number")) or 0,
            pr_number=_pull_request_number(remote),
            event=_clean_str(remote.get("event")),
            branch=_clean_str(remote.get("head_branch")),
            commit_sha=_clean_str(remote.get("head_sha")),
            commit_message=_commit_message(remote),
            display_title=_display_title(remote),
            head_repo=_head_repo(remote),
            commit_time=_commit_time(remote),
            commit_author=_commit_author(remote),
            base_ref=_base_ref(remote),
            actor=_actor(remote),
        )
        try:
            run.save()
        except IntegrityError:
            # Two deliveries of the same run can adopt it at the same moment --
            # job events of one run all arrive together, and each notices the
            # run missing before any of them inserts it. The loser re-reads the
            # winner's row instead of failing the whole delivery.
            existing = cls.find_one(
                (cls.c.workflow_id == run.workflow_id) & (cls.c.github_run_id == run.github_run_id)
            )
            if existing is None:
                raise
            return existing
        logger.info("Adopted run %s from '%s'", run.github_run_id, repo.full_name)
        # The execution this payload describes gets its own row, so the run's
        # timestamps and its attempt's timestamps are two facts rather than one
        # overwritten by the next re-run.
        from .attempt import RunAttempt

        attempt = RunAttempt.upsert_from_remote(run, remote)
        run._apply_attempt_state(attempt)
        return run

    def update_from_remote(self, remote: Mapping[str, Any]) -> WorkflowRun:
        """Apply a remote run payload to this stored record.

        Only attempt-independent metadata lands on the run row: the execution
        a payload describes -- its status, conclusion and timestamps -- is
        written to ``ct_run_attempts``, which is the single record of it. A
        late payload for an older attempt therefore updates that attempt's row
        and cannot rewrite the run's current state.
        """
        self.run_number = _as_int(remote.get("run_number")) or self.run_number
        # The attempt this payload describes must have a row of its own, filled
        # from *this* payload -- not a minimal row the next payload would have to
        # correct. A re-run's job payloads arrive for the new number before
        # anything has stored it, and jobs hang off that row.
        from .attempt import RunAttempt

        attempt = RunAttempt.upsert_from_remote(self, remote)
        pr_number = _pull_request_number(remote)
        if pr_number is not None:
            self.pr_number = pr_number
        self.branch = _clean_str(remote.get("head_branch"), self.branch)
        self.commit_sha = _clean_str(remote.get("head_sha"), self.commit_sha)

        message = _commit_message(remote)
        if message:
            self.commit_message = message
        self.display_title = _clean_str(remote.get("display_title"), self.display_title)
        self.head_repo = _head_repo(remote) or self.head_repo
        commit_time = _commit_time(remote)
        if commit_time is not None:
            self.commit_time = commit_time
        self.commit_author = _commit_author(remote) or self.commit_author
        self.base_ref = _base_ref(remote) or self.base_ref
        actor = _actor(remote)
        if actor:
            self.actor = actor

        if self.is_dirty:
            self.save()
        self._apply_attempt_state(attempt)
        return self

    def sync_jobs_from_remote(self, remote_jobs: Sequence[Mapping[str, Any]]) -> list[int]:
        """Store a batch of job payloads under their own attempts, and return their ids.

        The write half of the read cascade: ``ct_jobs.attempt_id`` → ``attempts``
        → ``run``, and each job cascades its ``steps``. Doing it here rather than
        in each caller is what keeps the three write paths (the pull channel, a
        webhook delivery, and the query path's lazy sync) from each carrying
        their own loop -- and their own idea of which attempt a job belongs to.

        Each job's attempt is resolved from the payload, so a run whose jobs
        span two attempts stores both, and a job is never filed under the wrong
        one because a caller guessed.

        Args:
            remote_jobs: GitHub job payloads (REST objects or webhook
                fragments), each optionally carrying a ``steps`` array.

        Returns:
            Local ids of the jobs stored, in payload order, skipping any that
            could not be persisted.
        """

        ids: list[int] = []
        for job_data in remote_jobs:
            job = Job.upsert_from_remote(self, job_data)
            if job.id is not None:
                ids.append(job.id)
        return ids

    def attempt_ids(self) -> list[int]:
        """Local ids of every stored attempt of this run, number ascending.

        One small query, reused by every two-hop job read: a run holds a handful
        of attempts, so doing this first keeps the job query to a single
        ``WHERE attempt_id IN (...)`` instead of a join the ORM would have to
        express for us.
        """
        from .attempt import RunAttempt

        if self.id is None:
            return []
        rows = (
            RunAttempt.query()
            .select(RunAttempt.c.id, RunAttempt.c.attempt_number)
            .where(RunAttempt.c.run_id == self.id)
            .order_by((RunAttempt.c.attempt_number, "ASC"))
            .all()
        )
        return [row.id for row in rows if row.id is not None]

    @classmethod
    def belongs_to_pull_request(
        cls, payload: Mapping[str, Any], number: int, head_sha: str
    ) -> bool:
        """Whether a run payload belongs to pull request ``number``.

        See :func:`_belongs_to_pull_request` for the two attribution tiers.
        """
        return _belongs_to_pull_request(payload, number, head_sha)

    def jobs_of_attempt(self, attempt: int | None = None) -> list[Job]:
        """Return the stored jobs of one attempt, in local id order.

        A re-run reuses the run id and increments ``run_attempt``, and job ids
        differ per attempt, so the table accumulates jobs from every attempt.
        Everything that reports "the jobs of this run" means the latest attempt
        -- that is what GitHub itself returns from ``/runs/{id}/jobs``.

        Args:
            attempt: Attempt number to select; ``None`` uses this run's own
                attempt.

        Returns:
            The jobs of that attempt (empty when nothing is stored for it).
        """
        from .attempt import RunAttempt

        if self.id is None:
            return []
        if attempt is None:
            attempt_row = RunAttempt.latest_for_run(self.id)
        else:
            attempt_row = RunAttempt.find_attempt(self.id, attempt)
        if attempt_row is None or attempt_row.id is None:
            return []
        return (
            Job.query().where(Job.c.attempt_id == attempt_row.id).order_by((Job.c.id, "ASC")).all()
        )

    # ---- shared internals -----------------------------------------------------

    def _stored_record(self) -> WorkflowRun | None:
        """The persisted record of this run's GitHub id, if any."""
        cls = type(self)
        return cls.find_one(cls.c.github_run_id == self.github_run_id)


class AsyncWorkflowRun(_AsyncRunStateFields, DefaultTimestampMixin, AsyncActiveRecord):
    """A single workflow run, asynchronous twin.

    Behavior-identical to :class:`WorkflowRun`; every method performs the
    same requests and raises the same exceptions.
    """

    __table_name__ = "ct_workflow_runs"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    workflow_id: int = 0
    github_run_id: int = 0
    run_number: int = 0
    #: Cached pull request attribution. ``None`` never means "no PR": GitHub
    #: leaves ``pull_requests[]`` empty on fork PRs, so this is an accelerator
    #: only. Queries by PR resolve the head sha instead.
    pr_number: int | None = None
    event: str = ""
    branch: str = ""
    commit_sha: str = ""
    commit_message: str = ""
    #: GitHub's own one-line title for this run (commit subject, or ``title #n``).
    display_title: str = ""
    #: The repository the code came from; differs from the subscription on a fork.
    head_repo: str = ""
    #: The commit's timestamp, which is not when CI started.
    commit_time: datetime | None = None
    #: The commit author, which is not who triggered the run.
    commit_author: str = ""
    #: Target branch of the pull request, when the run has one.
    base_ref: str = ""
    actor: str = ""
    #: Last webhook delivery that touched this row.
    webhook_received_at: datetime | None = None

    #: Owning workflow (pairs with :attr:`AsyncWorkflow.runs`).
    workflow: ClassVar[AsyncBelongsTo[AsyncWorkflow]] = AsyncBelongsTo(
        foreign_key="workflow_id", inverse_of="runs"
    )
    #: Every stored execution of this run, in attempt-number order. A job no
    #: longer hangs off the run directly (``attempt_id -> run_id`` would be a
    #: BCNF violation), so reading the run's jobs goes through these.
    attempts: ClassVar[AsyncOrderedHasMany[AsyncRunAttempt]] = AsyncOrderedHasMany(
        foreign_key="run_id",
        inverse_of="run",
        order_key=lambda attempt: attempt.attempt_number,
    )

    # ---- derived: the state of the newest attempt ---------------------------
    #
    # See :class:`WorkflowRun`: the run row is identity plus metadata, and the
    # current state is computed from the newest attempt at query time.

    @classmethod
    def _latest_column(cls, dialect: Any, attribute: str) -> Any:
        """Correlated scalar subquery: one column of this run's newest attempt.

        See :meth:`WorkflowRun._latest_column`; the async twin.
        """
        column = getattr(AsyncRunAttempt.c, attribute)
        inner = (
            AsyncRunAttempt.query()
            .select(column)
            .where(AsyncRunAttempt.c.run_id == cls.c.id)
            .order_by((AsyncRunAttempt.c.attempt_number, "DESC"))
            .limit(1)
        )
        return Subquery(dialect, inner.to_sql())

    def _apply_attempt_state(self, attempt: RunAttempt | None) -> None:
        """Expose an attempt's state through the derived fields, in memory.

        A write path already has the attempt row in hand; re-reading the run
        through a ``derived`` query just to fill these attributes would
        cost one query per delivery. ``None`` leaves them unset.
        """
        if attempt is None:
            return
        self.__dict__["latest_attempt"] = attempt.attempt_number
        self.__dict__["latest_status"] = attempt.status
        self.__dict__["latest_conclusion"] = attempt.conclusion
        self.__dict__["latest_started_at"] = attempt.started_at
        self.__dict__["latest_completed_at"] = attempt.completed_at
        self.__dict__["latest_github_created_at"] = attempt.github_created_at

    # ---- query domain -------------------------------------------------------

    @classmethod
    async def find_by_github_id(
        cls, github_run_id: int, *, derived: bool = False
    ) -> AsyncWorkflowRun | None:
        """Return the stored record of a GitHub run id, or ``None``.

        See :meth:`WorkflowRun.find_by_github_id`; ``derived`` loads the newest
        attempt's state in the same query.
        """
        return await cls.find_one(cls.c.github_run_id == github_run_id, derived=derived)

    @classmethod
    def recent(
        cls,
        workflow_ids: list[int],
        branch: str | None = None,
    ) -> AsyncActiveQuery:
        """Query builder over the runs of ``workflow_ids``, newest first.

        See :meth:`WorkflowRun.recent`; no ``status`` filter, for the same
        reason.
        """
        query = cls.query().where(cls.c.workflow_id.in_(workflow_ids))
        if branch:
            query = query.where(cls.c.branch == branch)
        return query.order_by((cls.c.run_number, "DESC"))

    @classmethod
    async def latest_by_branch(
        cls, workflow_ids: list[int], branch: str
    ) -> AsyncWorkflowRun | None:
        """Return the newest run of ``workflow_ids`` on ``branch``, or ``None``."""
        if not workflow_ids:
            return None
        return await cls.recent(workflow_ids, branch=branch).one()

    @classmethod
    async def latest_per_workflow(
        cls, workflow_ids: list[int], k: int = 1
    ) -> list[AsyncWorkflowRun]:
        """Return the ``k`` newest runs of every workflow in ``workflow_ids``.

        See :meth:`WorkflowRun.latest_per_workflow`; the single-query plus
        Python dedup approach is identical.
        """
        if not workflow_ids:
            return []
        rows = await cls._build_latest_per_workflow_query(workflow_ids, k).all()
        counts: dict[int, int] = {}
        result: list[AsyncWorkflowRun] = []
        for row in rows:
            wid = row.workflow_id
            counts[wid] = counts.get(wid, 0) + 1
            if counts[wid] <= k:
                result.append(row)
        return result

    @classmethod
    def _build_latest_per_workflow_query(cls, workflow_ids: list[int], k: int) -> AsyncActiveQuery:
        """Build a query returning all runs for the given workflows.

        The library's ``SetOperationQuery`` renders UNION ALL operands without
        parentheses, so branches containing ``ORDER BY ... LIMIT`` produce
        invalid SQL. This method returns a plain query instead; the caller
        deduplicates per workflow in Python.
        """
        return (
            cls.query()
            .where(cls.c.workflow_id.in_(workflow_ids))
            .order_by((cls.c.run_number, "DESC"))
        )

    @classmethod
    async def status_summary(cls, workflow_ids: list[int]) -> list[dict[str, Any]]:
        """Aggregate run counts per workflow, by each run's newest attempt.

        See :meth:`WorkflowRun.status_summary`; the same two queries and the
        same keys.
        """
        if not workflow_ids:
            return []

        from .attempt import AsyncRunAttempt

        runs = await (
            cls.query()
            .select(cls.c.id, cls.c.workflow_id)
            .where(cls.c.workflow_id.in_(workflow_ids))
            .all()
        )
        by_run: dict[int, int] = {row.id: row.workflow_id for row in runs if row.id is not None}
        if not by_run:
            return []

        attempts = await (
            AsyncRunAttempt.query()
            .select(
                AsyncRunAttempt.c.run_id,
                AsyncRunAttempt.c.attempt_number,
                AsyncRunAttempt.c.status,
                AsyncRunAttempt.c.conclusion,
            )
            .where(AsyncRunAttempt.c.run_id.in_(list(by_run)))
            .order_by((AsyncRunAttempt.c.run_id, "ASC"))
            .order_by((AsyncRunAttempt.c.attempt_number, "DESC"))
            .all()
        )
        newest: dict[int, tuple[str, str | None]] = {}
        for row in attempts:
            newest.setdefault(row.run_id, (row.status, row.conclusion))

        counts: dict[int, dict[str, Any]] = {}
        for run_id, workflow_id in by_run.items():
            entry = counts.setdefault(
                workflow_id,
                {
                    "workflow_id": workflow_id,
                    "total": 0,
                    "completed": 0,
                    "success": 0,
                    "failure": 0,
                    "in_progress": 0,
                },
            )
            entry["total"] += 1
            status, conclusion = newest.get(run_id, ("queued", None))
            if status == "completed":
                entry["completed"] += 1
                if conclusion == "success":
                    entry["success"] += 1
                elif conclusion == "failure":
                    entry["failure"] += 1
            elif status == "in_progress":
                entry["in_progress"] += 1
        return list(counts.values())

    # ---- behavior -------------------------------------------------------------

    @classmethod
    async def adopt_from_remote(
        cls, repo: AsyncRepository, remote: Mapping[str, Any]
    ) -> AsyncWorkflowRun:
        """Store a run that only exists on GitHub so far.

        See :meth:`WorkflowRun.adopt_from_remote`; the workflow upsert and
        the ``run_started_at`` / ``started_at`` dual key are identical.
        """
        from .workflow import AsyncWorkflow

        workflow = AsyncWorkflow(
            repository_id=repo.id or 0,
            workflow_id=_as_int(remote.get("workflow_id")) or 0,
            name=_clean_str(remote.get("name")),
            path=_clean_str(remote.get("path")),
            state="active",
        )
        workflow = await workflow.upsert_from_remote(remote)

        run = cls(
            workflow_id=workflow.id or 0,
            github_run_id=_as_int(remote.get("id")) or 0,
            run_number=_as_int(remote.get("run_number")) or 0,
            pr_number=_pull_request_number(remote),
            event=_clean_str(remote.get("event")),
            branch=_clean_str(remote.get("head_branch")),
            commit_sha=_clean_str(remote.get("head_sha")),
            commit_message=_commit_message(remote),
            display_title=_display_title(remote),
            head_repo=_head_repo(remote),
            commit_time=_commit_time(remote),
            commit_author=_commit_author(remote),
            base_ref=_base_ref(remote),
            actor=_actor(remote),
        )
        try:
            await run.save()
        except IntegrityError:
            # See :meth:`WorkflowRun.adopt_from_remote`: several job deliveries
            # of one run can reach the insert together.
            existing = await cls.find_one(
                (cls.c.workflow_id == run.workflow_id) & (cls.c.github_run_id == run.github_run_id)
            )
            if existing is None:
                raise
            return existing
        logger.info("Adopted run %s from '%s'", run.github_run_id, repo.full_name)
        # The execution this payload describes gets its own row -- see the sync
        # twin for why the two timestamps must stay two facts.
        from .attempt import AsyncRunAttempt

        attempt = await AsyncRunAttempt.upsert_from_remote(run, remote)
        run._apply_attempt_state(attempt)
        return run

    async def update_from_remote(self, remote: Mapping[str, Any]) -> AsyncWorkflowRun:
        """Apply a remote run payload to this stored record.

        See :meth:`WorkflowRun.update_from_remote`: only attempt-independent
        metadata lands here; the execution's own state goes to its attempt row.
        """
        self.run_number = _as_int(remote.get("run_number")) or self.run_number
        # The attempt this payload describes must have a row of its own, or the
        # execution it names is invisible: jobs hang off the attempt, and a
        # re-run's job payloads arrive for the *new* number before anything has
        # stored it.
        from .attempt import AsyncRunAttempt

        attempt = await AsyncRunAttempt.upsert_from_remote(self, remote)
        pr_number = _pull_request_number(remote)
        if pr_number is not None:
            self.pr_number = pr_number
        self.branch = _clean_str(remote.get("head_branch"), self.branch)
        self.commit_sha = _clean_str(remote.get("head_sha"), self.commit_sha)

        message = _commit_message(remote)
        if message:
            self.commit_message = message
        self.display_title = _clean_str(remote.get("display_title"), self.display_title)
        self.head_repo = _head_repo(remote) or self.head_repo
        commit_time = _commit_time(remote)
        if commit_time is not None:
            self.commit_time = commit_time
        self.commit_author = _commit_author(remote) or self.commit_author
        self.base_ref = _base_ref(remote) or self.base_ref
        actor = _actor(remote)
        if actor:
            self.actor = actor

        if self.is_dirty:
            await self.save()
        self._apply_attempt_state(attempt)
        return self

    @classmethod
    def belongs_to_pull_request(
        cls, payload: Mapping[str, Any], number: int, head_sha: str
    ) -> bool:
        """Whether a run payload belongs to pull request ``number``.

        See :func:`_belongs_to_pull_request` for the two attribution tiers.
        """
        return _belongs_to_pull_request(payload, number, head_sha)

    async def jobs_of_attempt(self, attempt: int | None = None) -> list[AsyncJob]:
        """Return the stored jobs of one attempt, in local id order.

        See :meth:`WorkflowRun.jobs_of_attempt`; the selection and ordering are
        identical.
        """
        from .attempt import AsyncRunAttempt

        if self.id is None:
            return []
        if attempt is None:
            attempt_row = await AsyncRunAttempt.latest_for_run(self.id)
        else:
            attempt_row = await AsyncRunAttempt.find_attempt(self.id, attempt)
        if attempt_row is None or attempt_row.id is None:
            return []
        # ``with_("steps")`` loads every job's steps in one extra query; asking
        # each job for its steps afterwards would be one query per job.
        return await (
            AsyncJob.query()
            .where(AsyncJob.c.attempt_id == attempt_row.id)
            .order_by((AsyncJob.c.id, "ASC"))
            .with_("steps")
            .all()
        )

    async def sync_jobs_from_remote(self, remote_jobs: Sequence[Mapping[str, Any]]) -> list[int]:
        """Store a batch of job payloads under their own attempts.

        See :meth:`WorkflowRun.sync_jobs_from_remote`; the async twin, same
        behaviour and same ordering guarantees.
        """

        ids: list[int] = []
        for job_data in remote_jobs:
            job = await AsyncJob.upsert_from_remote(self, job_data)
            if job.id is not None:
                ids.append(job.id)
        return ids

    async def attempt_ids(self) -> list[int]:
        """Local ids of every stored attempt of this run, number ascending.

        See :meth:`WorkflowRun.attempt_ids`; the async twin.
        """
        from .attempt import AsyncRunAttempt

        if self.id is None:
            return []
        rows = await (
            AsyncRunAttempt.query()
            .select(AsyncRunAttempt.c.id, AsyncRunAttempt.c.attempt_number)
            .where(AsyncRunAttempt.c.run_id == self.id)
            .order_by((AsyncRunAttempt.c.attempt_number, "ASC"))
            .all()
        )
        return [row.id for row in rows if row.id is not None]

    # ---- shared internals -----------------------------------------------------

    async def _stored_record(self) -> AsyncWorkflowRun | None:
        """The persisted record of this run's GitHub id, if any."""
        cls = type(self)
        return await cls.find_one(cls.c.github_run_id == self.github_run_id)
