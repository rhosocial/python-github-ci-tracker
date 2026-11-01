# tests/app/test_workflow_files.py
"""The workflow files parse into the shape their author intended.

A duplicate mapping key is the one YAML mistake a parser will not report:
``yaml.safe_load`` keeps the *last* value and says nothing. Losing a ``steps:``
list that way takes the checkouts, the dependency install and the interpreter
setup with it, and the job then fails in a way that has nothing to do with the
cause -- or, worse, passes because the surviving steps happen to be the cheap
ones.

Seen once: a ``services:`` block was inserted immediately before the *Lint*
step, which closed the step list, opened a second ``steps:`` under the same job,
and left the job with four steps and no checkout. ``yaml.safe_load`` accepted it
and reported nothing; GitHub replied with a run that had no jobs at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

#: Every workflow this repository runs.
WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

#: Keys whose disappearance would silently delete work from a job.
REQUIRED_KEYS = ("steps",)


def _child(node: Any, name: str) -> Any:
    """The value of ``name`` inside a mapping node, or ``None``."""
    for key_node, value_node in node.value:
        if getattr(key_node, "value", None) == name:
            return value_node
    return None


def _job_keys(path: Path) -> dict[str, list[str]]:
    """Each job name and the keys it carries, in document order.

    ``yaml.compose`` keeps both duplicates, unlike ``safe_load``, which is why a
    duplicate shows up here as the same key twice under one job.
    """
    with path.open(encoding="utf-8") as handle:
        root = yaml.compose(handle, Loader=yaml.SafeLoader)

    jobs = _child(root, "jobs")
    report: dict[str, list[str]] = {}
    for job_name_node, job_node in jobs.value:
        job_name = getattr(job_name_node, "value", "")
        keys = report.setdefault(job_name, [])
        for key_node, _ in job_node.value:
            key = getattr(key_node, "value", "")
            if isinstance(key, str):
                keys.append(key)
    return report


def _workflows() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


def test_no_job_repeats_a_key() -> None:
    """A repeated ``steps``/``services`` would silently delete the earlier one."""
    for path in _workflows():
        for job, keys in _job_keys(path).items():
            duplicates = sorted({key for key in keys if keys.count(key) > 1})
            assert duplicates == [], (
                f"{path.name}: job '{job}' repeats {duplicates}; a loader keeps "
                "only the last one, so the rest of that job's configuration is "
                "dropped without a warning"
            )


def test_every_job_declares_steps() -> None:
    """A job with no steps is a run that never starts, not an empty test."""
    for path in _workflows():
        for job, keys in _job_keys(path).items():
            assert all(
                key in keys for key in REQUIRED_KEYS
            ), f"{path.name}: job '{job}' lacks {REQUIRED_KEYS}"
