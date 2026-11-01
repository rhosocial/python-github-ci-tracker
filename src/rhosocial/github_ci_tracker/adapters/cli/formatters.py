# src/rhosocial/github_ci_tracker/adapters/cli/formatters.py
"""Output formatters for CLI."""

from __future__ import annotations

import json
from typing import Any


def format_output(data: Any, fmt: str = "text", verbose: bool = False) -> str:
    """Format output data.

    Args:
        data: Data to format
        fmt: Output format ('text' or 'json')
        verbose: Whether to include verbose output

    Returns:
        Formatted string
    """
    if fmt == "json":
        return json.dumps(data, indent=2, default=str, ensure_ascii=False)
    return format_text(data, verbose)


def format_text(data: Any, verbose: bool = False) -> str:
    """Format data as text."""
    if isinstance(data, dict):
        return _format_dict(data, verbose)
    elif isinstance(data, list):
        return _format_list(data, verbose)
    return str(data)


def _format_dict(data: dict[str, Any], verbose: bool) -> str:
    """Format a dictionary."""
    lines = []
    for key, value in data.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {format_text(item, verbose)}")
        elif isinstance(value, dict):
            lines.append(f"{key}:")
            for k, v in value.items():
                lines.append(f"  {k}: {v}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _format_list(data: list[Any], verbose: bool) -> str:
    """Format a list."""
    if not data:
        return "(empty)"

    if isinstance(data[0], dict):
        return _format_table(data, verbose)

    return "\n".join(str(item) for item in data)


def _format_table(data: list[dict[str, Any]], verbose: bool) -> str:
    """Format list of dicts as a table."""
    if not data:
        return "(empty)"

    columns = list(data[0].keys())

    widths = {}
    for col in columns:
        widths[col] = max(
            len(str(col)),
            max(len(str(row.get(col, ""))) for row in data),
        )

    header = " | ".join(str(col).ljust(widths[col]) for col in columns)
    separator = "-+-".join("-" * widths[col] for col in columns)

    rows = []
    for row in data:
        rows.append(" | ".join(str(row.get(col, "")).ljust(widths[col]) for col in columns))

    return "\n".join([header, separator] + rows)


def format_trigger_result(result: dict[str, Any], fmt: str = "text") -> str:
    """Format trigger workflow result."""
    if fmt == "json":
        return json.dumps(result, indent=2)

    lines = [
        f"Run ID: {result['run_id']}",
        f"Status: {result['status']}",
        f"Message: {result['message']}",
    ]
    if result.get("html_url"):
        lines.append(f"URL: {result['html_url']}")
    return "\n".join(lines)


def format_run_status(status: dict[str, Any], fmt: str = "text") -> str:
    """Format run status."""
    if fmt == "json":
        return json.dumps(status, indent=2, default=str)

    lines = [
        f"Run ID: {status['run_id']}",
        f"Status: {status['status']}",
    ]
    if status.get("conclusion"):
        lines.append(f"Conclusion: {status['conclusion']}")
    if status.get("branch"):
        lines.append(f"Branch: {status['branch']}")
    if status.get("duration_seconds"):
        lines.append(f"Duration: {status['duration_seconds']}s")
    if status.get("jobs"):
        lines.append("")
        lines.append("Jobs:")
        for job in status["jobs"]:
            lines.append(f"  - {job['name']}: {job['status']} ({job.get('conclusion', 'N/A')})")
    return "\n".join(lines)
