# src/rhosocial/github_ci_tracker/schema/model.py
"""The schema as data: tables, columns, constraints, indexes.

This module holds *what* the schema is, with no dialect and no SQL text. It
exists so the same definition can be rendered two ways:

* :mod:`schema.ddl` renders it as plain SQL text -- the fallback that works
  against any backend the project might use, and the thing a human reads when
  reviewing a change;
* :mod:`schema.ddl_postgres` renders it through the Postgres expression system,
  which is what the service actually executes.

Keeping the intent as data is what makes the two renderings provably the same
schema rather than two strings that happen to agree today. It also settles the
question of whether the expression system can express this schema: if a feature
cannot be described here, it cannot be rendered there.

Column types are spelled with the small enum below, not with SQL names, because
``SERIAL`` is Postgres-only while ``INTEGER`` and identity are portable; the
renderer decides how to honour ``autoincrement``.

**This schema is pre-production, so it may change shape.** Every table is applied
with ``CREATE TABLE IF NOT EXISTS`` and the project keeps no migrations, which
means a *new* table converges on the next boot while a *changed* table does not:
a running installation keeps the columns it was created with, and nothing reports
the drift. The deployment contract is therefore "an existing database is dropped
and recreated when the schema changes shape" -- verified against the current
installations, which hold no data worth preserving. A column can move between
tables, be renamed, or be dropped; a new *column* on an *existing* table is the
one change that silently does nothing.

An attempt is a first-class entity here rather than an integer on
``ct_jobs``: a re-run reuses the run id and increments the attempt number, so
the attempt is what a job actually belongs to. ``ct_jobs.attempt_id`` is a real
foreign key, which is only possible because of the paragraph above.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ColumnType(str, Enum):
    """Portable column types this schema needs.

    Deliberately small: a schema that uses three types is easier to check than
    one that can spell twelve. Adding a member means updating both renderers.
    """

    SERIAL = "serial"
    INTEGER = "integer"
    BIGINT = "bigint"
    VARCHAR = "varchar"
    TEXT = "text"
    BOOLEAN = "boolean"
    TIMESTAMPTZ = "timestamptz"
    JSONB = "jsonb"


class OnDelete(str, Enum):
    """Referential action for a foreign key."""

    CASCADE = "CASCADE"
    SET_NULL = "SET NULL"
    RESTRICT = "RESTRICT"


@dataclass(frozen=True)
class Column:
    """One column definition."""

    name: str
    type: ColumnType
    #: ``VARCHAR(n)`` length; ignored by other types.
    length: int | None = None
    nullable: bool = True
    primary_key: bool = False
    unique: bool = False
    #: Default value, as a Python value rather than SQL text.
    #:
    #: ``True``/``False``/``0``/``'main'`` are values the renderers know how to
    #: spell. ``NOW()`` is a *call*, not a value, so it is declared with
    #: :class:`DefaultNow` instead -- the difference has to be in the type,
    #: because a string ``"NOW()"`` reaches SQL as a quoted literal that silently
    #: stops being a timestamp.
    default: bool | int | str | DefaultNow | None = None


@dataclass(frozen=True)
class DefaultNow:
    """The current timestamp, as a default.

    A marker rather than a string so the renderers cannot mistake it for text.
    Postgres spells it ``NOW()``; another backend could spell it
    ``CURRENT_TIMESTAMP``, and the plain-SQL renderer already does.
    """


@dataclass(frozen=True)
class ForeignKey:
    """A single-column foreign key."""

    column: str
    references_table: str
    references_column: str = "id"
    on_delete: OnDelete | None = None


@dataclass(frozen=True)
class UniqueConstraint:
    """A table-level uniqueness constraint over one or more columns."""

    columns: tuple[str, ...]


@dataclass(frozen=True)
class Index:
    """A standalone index over one or more columns."""

    name: str
    columns: tuple[str, ...]
    unique: bool = False


@dataclass(frozen=True)
class Table:
    """One table, with everything needed to create it."""

    name: str
    columns: tuple[Column, ...]
    foreign_keys: tuple[ForeignKey, ...] = ()
    unique_constraints: tuple[UniqueConstraint, ...] = ()
    indexes: tuple[Index, ...] = ()


@dataclass(frozen=True)
class Schema:
    """A set of tables plus the names this schema used to have."""

    tables: tuple[Table, ...]
    #: Retired table names. A reset drops them so an upgraded database leaves no
    #: orphans behind; this project keeps no migrations.
    legacy_tables: tuple[str, ...] = ()
    #: Provider-owned tables, appended by the provider registry.
    provider_tables: tuple[Table, ...] = ()

    def all_tables(self) -> tuple[Table, ...]:
        """Core then provider tables, in creation order."""
        return (*self.tables, *self.provider_tables)

    def drop_order(self) -> tuple[str, ...]:
        """Table names in safe teardown order: dependants before their parents.

        The order is written down rather than inferred from the foreign keys,
        because a schema diff is easier to review when the sequence is explicit
        -- and because a reset must also drop tables whose keys are gone.
        """
        return (
            # No foreign keys: it can be dropped at any point.
            "ct_webhook_delivery",
            "ct_job_logs",
            "ct_step_logs",
            "ct_steps",
            # A job's foreign key points at its attempt, so the job goes first.
            "ct_jobs",
            "ct_run_attempts",
            "ct_workflow_runs",
            "ct_workflows",
            "ct_repository_bindings",
            "ct_repositories",
            "ct_pat_credentials",
        )


# ---------------------------------------------------------------------------
# The schema itself
# ---------------------------------------------------------------------------

_SERIAL = Column("id", ColumnType.SERIAL, primary_key=True, nullable=False)
_TIMESTAMP_CREATED = Column(
    "created_at", ColumnType.TIMESTAMPTZ, nullable=False, default=DefaultNow()
)
_TIMESTAMP_UPDATED = Column(
    "updated_at", ColumnType.TIMESTAMPTZ, nullable=False, default=DefaultNow()
)

CORE_SCHEMA = Schema(
    legacy_tables=("ct_github_tokens",),
    tables=(
        Table(
            name="ct_repositories",
            columns=(
                _SERIAL,
                Column("github_repo_id", ColumnType.BIGINT, nullable=False, unique=True),
                Column("full_name", ColumnType.VARCHAR, length=255, nullable=False),
                Column("owner", ColumnType.VARCHAR, length=100, nullable=False),
                Column("name", ColumnType.VARCHAR, length=100, nullable=False),
                Column("is_private", ColumnType.BOOLEAN, default=False),
                Column("default_branch", ColumnType.VARCHAR, length=100, default="main"),
                _TIMESTAMP_CREATED,
                _TIMESTAMP_UPDATED,
            ),
        ),
        Table(
            name="ct_repository_bindings",
            columns=(
                _SERIAL,
                Column("repository_id", ColumnType.INTEGER, nullable=False),
                Column("provider_kind", ColumnType.VARCHAR, length=32, nullable=False),
                Column("provider_ref", ColumnType.BIGINT, nullable=False),
                _TIMESTAMP_CREATED,
                _TIMESTAMP_UPDATED,
            ),
            foreign_keys=(
                ForeignKey("repository_id", "ct_repositories", on_delete=OnDelete.CASCADE),
            ),
            # One credential per repository: two providers can never both answer.
            unique_constraints=(UniqueConstraint(("repository_id",)),),
            indexes=(Index("idx_bindings_provider", ("provider_kind", "provider_ref")),),
        ),
        Table(
            name="ct_workflows",
            columns=(
                _SERIAL,
                Column("repository_id", ColumnType.INTEGER, nullable=False),
                Column("workflow_id", ColumnType.BIGINT, nullable=False),
                Column("name", ColumnType.VARCHAR, length=255, nullable=False),
                Column("path", ColumnType.VARCHAR, length=500, nullable=False),
                Column("state", ColumnType.VARCHAR, length=20, default="active"),
                _TIMESTAMP_CREATED,
                _TIMESTAMP_UPDATED,
            ),
            foreign_keys=(
                ForeignKey("repository_id", "ct_repositories", on_delete=OnDelete.CASCADE),
            ),
            unique_constraints=(UniqueConstraint(("repository_id", "workflow_id")),),
            indexes=(Index("idx_workflows_repository", ("repository_id",)),),
        ),
        Table(
            name="ct_workflow_runs",
            columns=(
                _SERIAL,
                Column("workflow_id", ColumnType.INTEGER, nullable=False),
                Column("github_run_id", ColumnType.BIGINT, nullable=False),
                Column("run_number", ColumnType.INTEGER, nullable=False),
                # Cached PR attribution; NULL means unknown, never "no PR".
                Column("pr_number", ColumnType.INTEGER),
                Column("event", ColumnType.VARCHAR, length=50, nullable=False),
                Column("branch", ColumnType.VARCHAR, length=255, nullable=False),
                Column("commit_sha", ColumnType.VARCHAR, length=40, nullable=False),
                Column("commit_message", ColumnType.TEXT),
                # GitHub's own title for this run: the commit subject on a push,
                # ``title #123`` on a pull request. It is what a caller reads to
                # tell two runs of one commit apart, and it costs one field.
                Column("display_title", ColumnType.VARCHAR, length=200, default=""),
                # The repository the code came from. Differs from the subscribed
                # repository for a fork pull request, which explains a missing
                # commit or missing secrets without another GitHub call.
                Column("head_repo", ColumnType.VARCHAR, length=255, default=""),
                # The commit's own timestamp / author. Distinct from the run's
                # ``created_at`` (when CI started) and from ``actor`` (who
                # triggered it): a run can sit on a three-day-old commit.
                Column("commit_time", ColumnType.TIMESTAMPTZ),
                Column("commit_author", ColumnType.VARCHAR, length=255, default=""),
                # Target branch of the pull request, when the run has one.
                Column("base_ref", ColumnType.VARCHAR, length=255, default=""),
                Column("actor", ColumnType.VARCHAR, length=100),
                # Last webhook delivery that touched this row: the push-channel
                # health signal.
                Column("webhook_received_at", ColumnType.TIMESTAMPTZ),
                _TIMESTAMP_CREATED,
                _TIMESTAMP_UPDATED,
            ),
            foreign_keys=(ForeignKey("workflow_id", "ct_workflows", on_delete=OnDelete.CASCADE),),
            unique_constraints=(UniqueConstraint(("workflow_id", "github_run_id")),),
            indexes=(
                Index("idx_runs_workflow", ("workflow_id",)),
                # "did this commit pass?" must be a local lookup.
                Index("idx_runs_commit", ("commit_sha",)),
                Index("idx_runs_pr", ("pr_number",)),
            ),
        ),
        Table(
            name="ct_run_attempts",
            columns=(
                _SERIAL,
                Column("run_id", ColumnType.INTEGER, nullable=False),
                # GitHub's attempt number, unique per run: a re-run increments it
                # while reusing the run id, so the pair is the natural key.
                Column("attempt_number", ColumnType.INTEGER, nullable=False, default=1),
                Column("status", ColumnType.VARCHAR, length=20, default="queued"),
                Column("conclusion", ColumnType.VARCHAR, length=20),
                # Who triggered this attempt: a re-run can be started by someone
                # other than the run's original author.
                Column("actor", ColumnType.VARCHAR, length=100),
                # GitHub's ``run_started_at`` / ``completed_at`` for this
                # attempt. Per execution, so they live here and not on the run.
                Column("started_at", ColumnType.TIMESTAMPTZ),
                Column("completed_at", ColumnType.TIMESTAMPTZ),
                # GitHub's own ``created_at`` / ``updated_at`` for this attempt.
                # Prefixed so they cannot be confused with the table's own
                # timestamps: the wall-clock of the attempt is not the wall-clock
                # of the row.
                Column("github_created_at", ColumnType.TIMESTAMPTZ),
                Column("github_updated_at", ColumnType.TIMESTAMPTZ),
                _TIMESTAMP_CREATED,
                _TIMESTAMP_UPDATED,
            ),
            foreign_keys=(ForeignKey("run_id", "ct_workflow_runs", on_delete=OnDelete.CASCADE),),
            # One row per execution of the run; a re-run must overwrite, not add.
            unique_constraints=(UniqueConstraint(("run_id", "attempt_number")),),
            indexes=(Index("idx_attempts_run", ("run_id",)),),
        ),
        Table(
            # A job belongs to an *attempt*, not to a run. GitHub reuses the run
            # id when a run is re-run and issues new job ids, so the attempt is
            # what a job actually hangs off -- and ``run_id`` is deliberately
            # absent: an attempt determines exactly one run, so carrying it here
            # would be a BCNF violation (``attempt_id -> run_id`` with
            # ``attempt_id`` not a superkey), leaving the two foreign keys free to
            # disagree. "The jobs of this run" is a two-hop read through
            # :meth:`ct_run_attempts`; see ``WorkflowRun.jobs_of_attempt``.
            name="ct_jobs",
            columns=(
                _SERIAL,
                # GitHub job ids are globally unique -- measured: attempt 1 of a
                # re-run used 121451607060, attempt 2 used 121481098854 -- so
                # this alone determines the row; the composite below is kept
                # because it makes the upsert lookup obvious.
                Column("github_job_id", ColumnType.BIGINT, nullable=False),
                Column("attempt_id", ColumnType.INTEGER, nullable=False),
                Column("name", ColumnType.VARCHAR, length=255, nullable=False),
                Column("status", ColumnType.VARCHAR, length=20, default="queued"),
                Column("conclusion", ColumnType.VARCHAR, length=20),
                Column("runner_name", ColumnType.VARCHAR, length=100),
                Column("started_at", ColumnType.TIMESTAMPTZ),
                Column("completed_at", ColumnType.TIMESTAMPTZ),
                _TIMESTAMP_CREATED,
                _TIMESTAMP_UPDATED,
            ),
            foreign_keys=(ForeignKey("attempt_id", "ct_run_attempts", on_delete=OnDelete.CASCADE),),
            unique_constraints=(UniqueConstraint(("attempt_id", "github_job_id")),),
            indexes=(Index("idx_jobs_attempt", ("attempt_id",)),),
        ),
        Table(
            name="ct_steps",
            columns=(
                _SERIAL,
                Column("job_id", ColumnType.INTEGER, nullable=False),
                Column("github_step_id", ColumnType.BIGINT, nullable=False),
                Column("name", ColumnType.VARCHAR, length=255, nullable=False),
                Column("status", ColumnType.VARCHAR, length=20, default="queued"),
                Column("conclusion", ColumnType.VARCHAR, length=20),
                Column("number", ColumnType.INTEGER, nullable=False),
                Column("started_at", ColumnType.TIMESTAMPTZ),
                Column("completed_at", ColumnType.TIMESTAMPTZ),
                _TIMESTAMP_CREATED,
                _TIMESTAMP_UPDATED,
            ),
            foreign_keys=(ForeignKey("job_id", "ct_jobs", on_delete=OnDelete.CASCADE),),
            unique_constraints=(UniqueConstraint(("job_id", "github_step_id")),),
            indexes=(Index("idx_steps_job", ("job_id",)),),
        ),
        Table(
            # What one step printed, when it was captured. One row per step at
            # most; the absence of a row is the normal case for a step whose run
            # finished more than about an hour ago, because GitHub keeps the
            # per-step files in the log archive for roughly that long and then
            # rewrites the archive flat. Nothing can be recovered afterwards, so
            # the missing row is an answer rather than a gap to fill later.
            #
            # Same one-to-one convention as ``ct_job_logs``: the optional side is
            # the absence of a row, never a nullable pointer on ``ct_steps`` --
            # a pointer would make the tables reference each other and force a
            # NULL update every time a log is purged.
            #
            # No chunking and no per-step text cap here; the archive's own file
            # is the unit, measured at 4KB median / 253KB largest for a 12-job
            # matrix run.
            name="ct_step_logs",
            columns=(
                _SERIAL,
                # UNIQUE is what makes this 1:1 rather than 1:N.
                Column("step_id", ColumnType.INTEGER, nullable=False, unique=True),
                Column("log_content", ColumnType.TEXT, nullable=False),
                # Denormalised at write time for the same reason as the job log:
                # "how many lines" is asked far more often than the text is read.
                Column("line_count", ColumnType.INTEGER, nullable=False, default=0),
                # When the archive was read. Steps of one run share a value,
                # which is what makes "these came from one capture" visible.
                Column("fetched_at", ColumnType.TIMESTAMPTZ),
                _TIMESTAMP_CREATED,
                _TIMESTAMP_UPDATED,
            ),
            foreign_keys=(ForeignKey("step_id", "ct_steps", on_delete=OnDelete.CASCADE),),
            indexes=(Index("idx_step_logs_step", ("step_id",)),),
        ),
        Table(
            # One log document per *job* -- GitHub's own bulk export packages
            # them this way (the run archive holds one file per job), and there
            # is no per-step endpoint on github.com. No chunking: a whole log
            # lives in one row, which PostgreSQL TOASTes out of line and
            # compressed -- a real 3.4MB log measured 376KB on disk and 223ms to
            # write, against 1489ms for the 33 sequential inserts chunking used
            # to cost.
            #
            # The "optional" side of this 1:1 is the *absence of a row*, not a
            # nullable ``ct_jobs.log_id``. A column on the job would have to be
            # nulled every time a log is purged, and it would make the two
            # tables reference each other -- a cyclic foreign key, which forces
            # an insert order and makes ON DELETE CASCADE ambiguous. Here the
            # job is untouched by a log deletion, and deleting the job still
            # takes its log with it.
            name="ct_job_logs",
            columns=(
                _SERIAL,
                # UNIQUE is what makes this 1:1 rather than 1:N, and it is a
                # candidate key in its own right.
                Column("job_id", ColumnType.INTEGER, nullable=False, unique=True),
                Column("log_content", ColumnType.TEXT, nullable=False),
                # Denormalised from the stored text at write time: "how many
                # lines does this log have" is asked far more often than the text
                # is read, and counting them means reading the text.
                Column("line_count", ColumnType.INTEGER, nullable=False, default=0),
                # When GitHub last handed us this text, so a caller can tell a
                # fresh log from one fetched before a re-run superseded it.
                Column("fetched_at", ColumnType.TIMESTAMPTZ),
                _TIMESTAMP_CREATED,
                _TIMESTAMP_UPDATED,
            ),
            foreign_keys=(ForeignKey("job_id", "ct_jobs", on_delete=OnDelete.CASCADE),),
            indexes=(Index("idx_job_logs_job", ("job_id",)),),
        ),
        Table(
            # One row per webhook delivery that reached the receiver. Transport
            # evidence, separate from the resource state in ct_workflow_runs /
            # ct_jobs: the two GitHub event streams are distinguished by
            # event_type rather than by two near-identical tables, and no
            # payload is copied -- delivery_id fetches it back from GitHub.
            name="ct_webhook_delivery",
            columns=(
                _SERIAL,
                # X-GitHub-Delivery. NOT unique: a redelivery re-sends the same
                # id, and that second arrival is what is_redelivery records.
                Column("delivery_id", ColumnType.VARCHAR, length=64, nullable=False),
                Column("event_type", ColumnType.VARCHAR, length=32, nullable=False),
                Column("received_at", ColumnType.TIMESTAMPTZ, nullable=False, default=DefaultNow()),
                Column("repository_full_name", ColumnType.VARCHAR, length=255),
                Column("action", ColumnType.VARCHAR, length=32),
                # Both ids are nullable: a signature failure carries none, and a
                # job delivery carries both.
                Column("github_run_id", ColumnType.BIGINT),
                Column("github_job_id", ColumnType.BIGINT),
                Column("handled", ColumnType.BOOLEAN, nullable=False, default=False),
                Column("reason", ColumnType.VARCHAR, length=64),
                Column("is_redelivery", ColumnType.BOOLEAN, nullable=False, default=False),
                _TIMESTAMP_CREATED,
                _TIMESTAMP_UPDATED,
            ),
            indexes=(
                # The health signal: newest delivery per stream, and the count
                # within a time window.
                Index("idx_delivery_stream_time", ("event_type", "received_at")),
                # Redelivery detection and "what happened to this delivery?".
                Index("idx_delivery_delivery_id", ("delivery_id",)),
                Index("idx_delivery_run", ("github_run_id",)),
            ),
        ),
    ),
)

#: The PAT provider's table. It lives here as data rather than in
#: ``credentials/pat`` so both renderers see one schema; the provider keeps its
#: own model and lifecycle.
PAT_SCHEMA = Table(
    name="ct_pat_credentials",
    columns=(
        _SERIAL,
        # Operator-facing alias; the CLI's primary key.
        Column("name", ColumnType.VARCHAR, length=100, nullable=False, unique=True),
        # The secret -- plaintext by design for a single-user self-hosted
        # deployment; see models/pat_credential.py for the trade-off.
        Column("token", ColumnType.TEXT, nullable=False, unique=True),
        Column("owner_login", ColumnType.VARCHAR, length=100),
        Column("scope", ColumnType.VARCHAR, length=100, default="repo,workflow"),
        Column("expires_at", ColumnType.TIMESTAMPTZ),
        Column("is_active", ColumnType.BOOLEAN, nullable=False, default=True),
        # Probe results, cached so gating a trigger needs no network round trip.
        Column("can_read_actions", ColumnType.BOOLEAN),
        Column("can_write_actions", ColumnType.BOOLEAN),
        Column("can_manage_webhooks", ColumnType.BOOLEAN),
        Column("capabilities_probed_at", ColumnType.TIMESTAMPTZ),
        _TIMESTAMP_CREATED,
        _TIMESTAMP_UPDATED,
    ),
    indexes=(Index("idx_pat_active", ("is_active",)),),
)

SCHEMA = Schema(
    tables=CORE_SCHEMA.tables,
    legacy_tables=CORE_SCHEMA.legacy_tables,
    provider_tables=(PAT_SCHEMA,),
)


def provider_table(name: str) -> Table:
    """Return a provider-owned table definition by name.

    Args:
        name: Table name, for example ``ct_pat_credentials``.

    Raises:
        KeyError: No provider table has that name.
    """
    for table in SCHEMA.provider_tables:
        if table.name == name:
            return table
    raise KeyError(f"no provider table named '{name}'")


__all__ = [
    "CORE_SCHEMA",
    "DefaultNow",
    "PAT_SCHEMA",
    "SCHEMA",
    "Column",
    "ColumnType",
    "ForeignKey",
    "Index",
    "OnDelete",
    "Schema",
    "Table",
    "UniqueConstraint",
    "provider_table",
]
