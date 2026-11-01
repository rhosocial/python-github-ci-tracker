# src/rhosocial/github_ci_tracker/services/github/logs.py
"""GitHub's run-log archive, read as data: which form is it, and what is inside.

``GET /repos/{owner}/{repo}/actions/runs/{run_id}/logs`` answers with a zip.
Its layout is **not documented**, and it is not constant: measured against real
runs (see ``.claude/plan/2026-10-31/step-log-investigation.md``), an archive
fetched soon after the run finished carries one file per step, while the same
run's archive a couple of hours later carries one merged file per job and no
step files at all. Nothing in the response says which form arrived; the only
honest way to tell is to look at what is inside.

Three kinds of entry exist, and only one of them is a reliable signal:

```
<job>/<step number>_<step name>.txt   step text   -- only while the archive is "hot"
<job>/system.txt                      runner scheduling notes -- present in both forms
<n>_<job name>.txt                    the merged job text -- present in both forms
```

So :attr:`RunLogArchive.form` is decided by the presence of step files, never by
``system.txt`` or the merged files. A run whose archive has step files for some
jobs but not others is reported per job (:meth:`steps_for` returns an empty
mapping for the rest); the caller decides what a partial capture means, because
"the run is still finishing" and "this job predates the split" are different
answers.

**The job log keeps its own source.** The merged file an archive carries for a
job is byte-for-byte the body of ``GET /actions/jobs/{job_id}/logs``: measured on
run 41067150299, 347 341 bytes and the same sha256 from both. So the archive is
not a replacement for that endpoint -- it is the only source of *step* files,
while job-level logs keep being fetched one job at a time, which costs a job's
bytes instead of a run's.

That also settles how the two stored tables relate: ``ct_step_logs`` is new and
can only be filled from here, inside the roughly one-hour window; ``ct_job_logs``
is unchanged, still filled by the single-job endpoint, and still what a caller
reads when no step files were captured.

This module holds no state and knows nothing about the database: it turns bytes
into entries. Storing them, and deciding whether a capture is good enough, is
the controller's business.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field

#: ``<job>/<number>_<name>.txt`` -- the only per-step entry GitHub emits.
_STEP_FILE = re.compile(r"^(?P<number>\d+)_(?P<name>.+)\.txt$")

#: ``<number>_<job>.txt`` at the top level: the merged job log, numbered by the
#: order GitHub happened to write it. The number means nothing to us.
_MERGED_FILE = re.compile(r"^\d+_(?P<job>.+)\.txt$")

#: The runner's own scheduling log. Parsed only to be skipped: the decision was
#: that it is not stored (it answers "why did the job wait", which the run and
#: job rows already answer with timestamps).
_SYSTEM_FILE = "system.txt"


@dataclass(frozen=True)
class StepFile:
    """One step's text, as the archive spells it."""

    job: str
    #: The step number, matching ``ct_steps.number`` for the same job.
    number: int
    #: The name GitHub put in the file name. Not necessarily the workflow's
    #: ``name:`` -- for a ``run:`` step it is the rendered command -- so callers
    #: must key on :attr:`number`, never on this.
    name: str
    content: str


@dataclass
class RunLogArchive:
    """A parsed run archive: merged job texts, and step files when present."""

    #: Merged text per job, keyed by the job name GitHub used in the archive.
    merged: dict[str, str] = field(default_factory=dict)
    #: Step files per job: ``{job name: {step number: StepFile}}``. Empty for a
    #: job that has none, which is the normal case once the archive is cold.
    steps: dict[str, dict[int, StepFile]] = field(default_factory=dict)
    #: Every job directory the archive contains, including one that holds only
    #: ``system.txt``. Lets a caller tell "the job is missing" from "the job is
    #: there but has no step files".
    dirs: set[str] = field(default_factory=set)

    @classmethod
    def from_bytes(cls, raw: bytes) -> RunLogArchive:
        """Parse the archive GitHub served.

        Raises:
            zipfile.BadZipFile: The body is not a zip. That is a transport-level
                surprise, not an empty archive, and must not be mistaken for one.
        """
        archive = RunLogArchive()
        with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
            for entry in bundle.namelist():
                if entry.endswith("/"):
                    continue  # a directory marker, not a document
                if "/" in entry:
                    job, filename = entry.split("/", 1)
                    archive.dirs.add(job)
                    if filename == _SYSTEM_FILE:
                        continue  # deliberately not stored
                    match = _STEP_FILE.match(filename)
                    if not match:
                        continue  # an unknown entry: ignore rather than guess
                    number = int(match.group("number"))
                    # First writer wins: GitHub does not repeat a step, and if it
                    # ever did, a stable pick beats "whichever came last".
                    archive.steps.setdefault(job, {}).setdefault(
                        number,
                        StepFile(
                            job=job,
                            number=number,
                            name=match.group("name"),
                            content=_decode(bundle.read(entry)),
                        ),
                    )
                    continue

                match = _MERGED_FILE.match(entry)
                if match:
                    job = match.group("job")
                    archive.merged.setdefault(job, _decode(bundle.read(entry)))
        return archive

    # -- what arrived ---------------------------------------------------------

    @property
    def form(self) -> str:
        """``"step"`` or ``"job"``, by structure alone.

        ``"step"`` means at least one step file is present; a per-job answer is
        :meth:`steps_for`, because one archive can hold both (a run whose last
        job is still finishing).
        """
        return "step" if self.steps else "job"

    @property
    def has_steps(self) -> bool:
        """Whether any job in this archive carries step files."""
        return bool(self.steps)

    # -- per job --------------------------------------------------------------

    def steps_for(self, job: str) -> dict[int, StepFile]:
        """The step files of one job, keyed by step number (empty when none)."""
        return self.steps.get(job, {})

    def merged_for(self, job: str) -> str | None:
        """The merged text of one job, or ``None`` when the archive lacks it."""
        return self.merged.get(job)

    def knows_job(self, job: str) -> bool:
        """Whether the archive has any entry for this job name."""
        return job in self.dirs or job in self.merged

    @property
    def job_names(self) -> set[str]:
        """Every job name the archive mentions, merged or directory only."""
        return set(self.dirs) | set(self.merged)


def _decode(raw: bytes) -> str:
    """A log file as text.

    GitHub writes UTF-8 with a BOM, and keeps it: the stored text is what the
    server served, the same convention the job log follows, and
    ``StepLog.lines()`` is what strips it for a caller. Some files also contain
    bytes that are not valid UTF-8 at all (a program printing raw binary);
    replacing those keeps the rest of the document readable, where a decode that
    raised would turn one stray byte into "this run has no logs".
    """
    return raw.decode("utf-8", errors="replace")


__all__ = ["RunLogArchive", "StepFile"]
