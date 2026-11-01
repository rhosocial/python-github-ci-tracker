# src/rhosocial/github_ci_tracker/adapters/mcp/logs.py
"""Bounded reading of a log document: line windows and pattern searches.

A log is text the caller asked to see; this module decides which lines come
back, never what they mean. It knows nothing about pytest, `##[error]`, test
summaries or a runner's teardown -- those are properties of one project's
output, and a general CI tracker that guesses at them is wrong for every project
that prints something else.

Three selections, all of them explicit or plainly bounded:

* ``tail`` / ``from_line`` / ``to_line`` -- a line window (1-based, inclusive);
* ``grep`` -- lines containing a caller-supplied pattern, with context;
* nothing at all -- the last :data:`DEFAULT_TAIL` lines.

Nothing here trusts the body: it is data, and every function treats it as text.

:func:`search_log` is the fourth entry point: it scans the whole document and
reports every match position, because the whole log lives in one row. Its line
numbers are absolute -- the caller can hand any of them to ``get_job_log`` as
``from_line`` and land on the same line, because both split the same string the
same way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Hard ceiling on the characters returned by one call, so a pathological
#: ``tail`` cannot blow up the caller's context.
MAX_CHARS = 20_000

#: Default number of trailing lines when the caller asks for nothing.
DEFAULT_TAIL = 200

#: Default number of matches whose context is returned by :func:`search_log`.
#: Beyond this, only the line numbers are reported: a job that fails 300 tests
#: would answer with 300 contexts and nothing else.
DEFAULT_MAX_MATCHES = 20

#: Hard ceiling on reported match positions, so a pathological pattern cannot
#: turn one call into a log dump.
MAX_MATCHES_REPORTED = 200

#: Why a search returned nothing.
SEARCH_EMPTY_LOG = "log_empty"
SEARCH_NO_MATCH = "no_match"

#: Why a selection came back empty. Stable strings: the tool publishes them and
#: a caller branches on them, so they are part of the contract.
EMPTY_LOG = "log_empty"
EMPTY_TAIL = "tail_not_positive"
EMPTY_WINDOW = "window_outside_document"
EMPTY_GREP = "grep_no_match"
EMPTY_FILTER = "filters_selected_nothing"


@dataclass
class LogWindow:
    """One windowed view of a log document."""

    #: The selected lines, joined with newlines.
    content: str = ""
    #: Total lines in the source document.
    total_lines: int = 0
    #: 1-based line number of the first returned line (0 when empty).
    first_line: int = 0
    #: 1-based line number of the last selected line (0 when empty). For a
    #: filtered selection this is the last matching line, not
    #: ``first_line + returned_lines``.
    last_line: int = 0
    #: How many lines :attr:`content` actually holds.
    returned_lines: int = 0
    #: Whether more lines exist outside the returned window.
    truncated: bool = False
    #: Matched line numbers when ``grep`` was used.
    matches: list[int] | None = None
    #: Why nothing was selected, when nothing was.
    empty_reason: str | None = None


def _split(text: str) -> list[str]:
    """Split a log into lines without inventing a trailing empty line.

    The whole document is one string, so ``splitlines`` is the whole story: no
    boundary can fall inside a line. A stored log does start with a BOM, which
    is stripped here rather than at storage time so that a caller comparing the
    text against what GitHub served sees the same bytes.
    """
    if not text:
        return []
    return text.lstrip("\ufeff").splitlines()


@dataclass
class LogSearch:
    """The result of searching a whole log document."""

    #: The pattern that was searched for, echoed so the payload is readable.
    query: str = ""
    #: Lines in the whole document.
    total_lines: int = 0
    #: How many lines matched, before any cap.
    total_matches: int = 0
    #: Line numbers of the matches reported (a prefix of all of them).
    matches: list[int] = field(default_factory=list)
    #: Context around the reported matches, with line numbers implied by
    #: ``first_line`` / ``last_line``.
    content: str = ""
    #: 1-based line number of the first returned line (0 when empty).
    first_line: int = 0
    last_line: int = 0
    #: Whether matches were left unreported.
    truncated_matches: bool = False
    #: Whether the content hit the character ceiling.
    truncated_content: bool = False
    #: Why nothing was returned.
    empty_reason: str | None = None


def search_log(
    content: str,
    query: str,
    *,
    context: int = 2,
    max_matches: int = DEFAULT_MAX_MATCHES,
    ignore_case: bool = False,
    max_chars: int = MAX_CHARS,
) -> LogSearch:
    """Search a whole document and return matches with context.

    Unlike ``window_log``'s ``grep``, this scans everything and caps by *match
    count* rather than by which window happened to be returned. It answers
    "everywhere X appears" in one call, which is what a model debugging a 3MB
    log needs; ``get_job_log`` then reads around any line number reported.

    Args:
        content: The raw log document.
        query: Regular expression, falling back to a literal search when the
            pattern is invalid.
        context: Lines of context kept around each match.
        max_matches: How many matches get context text.
        ignore_case: Match case-insensitively.
        max_chars: Ceiling on the returned context.

    Returns:
        A :class:`LogSearch`.
    """
    lines = _split(content)
    total = len(lines)
    if total == 0:
        return LogSearch(query=query, total_lines=0, empty_reason=SEARCH_EMPTY_LOG)

    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(query, flags)
    except re.error:
        regex = re.compile(re.escape(query), flags)

    matches = [index for index, line in enumerate(lines, start=1) if regex.search(line)]
    if not matches:
        return LogSearch(query=query, total_lines=total, empty_reason=SEARCH_NO_MATCH)

    reported_cap = min(max(1, max_matches), MAX_MATCHES_REPORTED)
    reported = matches[:reported_cap]
    keep: set[int] = set()
    for number in reported:
        keep.update(range(max(1, number - context), min(total, number + context) + 1))

    ordered = sorted(keep)
    returned = _fit_to_chars(lines, ordered, max_chars, keep_newest=False)
    body = "\n".join(lines[number - 1] for number in returned)
    truncated_content = len(returned) < len(ordered)
    if len(body) > max_chars:
        body = body[:max_chars]
        truncated_content = True

    # `matches` is capped only by MAX_MATCHES_REPORTED: the caller asked for
    # `max_matches` contexts, but every position is worth reporting, because a
    # number is cheap and a page is not.
    positions = matches[:MAX_MATCHES_REPORTED]

    return LogSearch(
        query=query,
        total_lines=total,
        total_matches=len(matches),
        matches=positions,
        content=body,
        first_line=returned[0],
        last_line=returned[-1],
        truncated_matches=len(positions) < len(matches),
        truncated_content=truncated_content,
    )


def _grep(
    lines: list[str],
    pattern: str,
    context: int,
    ignore_case: bool,
) -> tuple[list[int], set[int]]:
    """Return matching 1-based line numbers plus the lines to keep around them."""
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error:
        # A pattern the model got wrong should not fail the tool: fall back to
        # a literal search so the call still returns something actionable.
        regex = re.compile(re.escape(pattern), flags)

    matches = [index for index, line in enumerate(lines, start=1) if regex.search(line)]
    keep: set[int] = set()
    for number in matches:
        low = max(1, number - context)
        high = min(len(lines), number + context)
        keep.update(range(low, high + 1))
    return matches, keep


def _fit_to_chars(
    lines: list[str], ordered: list[int], max_chars: int, *, keep_newest: bool
) -> list[int]:
    """The longest run of ``ordered`` that fits in ``max_chars``.

    ``keep_newest`` keeps the trailing lines, which is what "the end of the
    document" means when the caller asked for a tail. Otherwise the leading
    lines are kept, which is the only defensible reading of an explicit
    ``from_line`` window or a grep search.
    """
    sequence = list(reversed(ordered)) if keep_newest else ordered
    kept: list[int] = []
    size = 0
    for number in sequence:
        addition = len(lines[number - 1]) + (1 if kept else 0)
        if size + addition > max_chars:
            break
        size += addition
        kept.append(number)
    if not kept:
        kept = [sequence[0]]
    if keep_newest:
        kept.reverse()
    return kept


def window_log(
    content: str,
    *,
    tail: int | None = None,
    from_line: int | None = None,
    to_line: int | None = None,
    grep: str | None = None,
    context: int = 2,
    ignore_case: bool = False,
    max_chars: int = MAX_CHARS,
) -> LogWindow:
    """Select a bounded window of ``content``.

    Args:
        content: The raw log document.
        tail: Keep the last ``tail`` lines. Not applied when ``from_line`` /
            ``to_line`` are given, or when ``grep`` is used.
        from_line: First line to keep (1-based, inclusive).
        to_line: Last line to keep (1-based, inclusive).
        grep: Keep only lines matching this pattern (regex, falling back to a
            literal search when the pattern is invalid).
        context: Lines of context kept around each grep match.
        ignore_case: Match ``grep`` case-insensitively.
        max_chars: Truncate the joined result to at most this many characters.

    Returns:
        A :class:`LogWindow` describing what was returned and whether more lines
        remain outside it.
    """
    lines = _split(content)
    total = len(lines)
    if total == 0:
        return LogWindow(total_lines=0, empty_reason=EMPTY_LOG)

    selected: set[int] = set(range(1, total + 1))
    matches: list[int] | None = None
    empty_reason: str | None = None

    explicit_window = from_line is not None or to_line is not None
    if explicit_window:
        low = max(1, from_line or 1)
        high = min(total, to_line or total)
        selected &= set(range(low, high + 1))
        if not selected:
            empty_reason = EMPTY_WINDOW
    elif grep:
        matches, near = _grep(lines, grep, context, ignore_case)
        selected &= near
        if not selected:
            empty_reason = EMPTY_GREP
    elif tail is not None:
        if tail <= 0:
            return LogWindow(total_lines=total, truncated=True, empty_reason=EMPTY_TAIL)
        selected &= set(range(max(1, total - tail + 1), total + 1))
    else:
        # Nothing asked for: the end of the document, bounded. Not "the end of
        # what the job printed" -- deciding that would mean knowing what a job
        # prints, which is the project's business, not this service's.
        selected &= set(range(max(1, total - DEFAULT_TAIL + 1), total + 1))

    if not selected:
        return LogWindow(
            total_lines=total,
            truncated=False,
            matches=matches or [],
            empty_reason=empty_reason or EMPTY_FILTER,
        )

    ordered = sorted(selected)
    returned = ordered
    body = "\n".join(lines[number - 1] for number in ordered)
    truncated = len(ordered) < total

    if len(body) > max_chars:
        # A tail keeps its end; an explicit range or a grep keeps its beginning
        # (where the caller pointed). Either way the cap is not a guess.
        returned = _fit_to_chars(lines, ordered, max_chars, keep_newest=not explicit_window)
        body = "\n".join(lines[number - 1] for number in returned)
        if len(body) > max_chars:
            # A single line longer than the cap: cut inside it rather than
            # return a document bigger than promised.
            body = body[:max_chars]
        truncated = True

    return LogWindow(
        content=body,
        total_lines=total,
        first_line=returned[0],
        last_line=returned[-1],
        returned_lines=len(returned),
        truncated=truncated,
        matches=matches,
    )


def empty_explanation(reason: str | None, *, total_lines: int, grep: str | None) -> str:
    """Turn an :attr:`LogWindow.empty_reason` into what the caller should do.

    An empty result must never read as "this job printed nothing": the log may
    hold thousands of lines that a filter excluded, and a caller that concludes
    otherwise stops looking for the real failure.
    """
    if reason == EMPTY_LOG:
        return "The job produced no log text at all."
    if reason == EMPTY_TAIL:
        return "tail must be positive; nothing was selected."
    if reason == EMPTY_WINDOW:
        return (
            f"The requested range is outside the {total_lines} lines this job "
            "has. Call again without from_line/to_line to read the default window."
        )
    if reason == EMPTY_GREP:
        return (
            f"No line matched {grep!r} in {total_lines} lines. Try ignore_case=true, "
            "a shorter pattern, or read the default window to see the actual "
            "failure text."
        )
    return f"The filters together selected nothing out of {total_lines} lines."


def default_window(content: str) -> LogWindow:
    """The window used when the caller gives no selection at all."""
    return window_log(content)


__all__ = [
    "DEFAULT_MAX_MATCHES",
    "DEFAULT_TAIL",
    "EMPTY_FILTER",
    "EMPTY_GREP",
    "EMPTY_LOG",
    "EMPTY_TAIL",
    "EMPTY_WINDOW",
    "MAX_CHARS",
    "MAX_MATCHES_REPORTED",
    "LogSearch",
    "LogWindow",
    "SEARCH_EMPTY_LOG",
    "SEARCH_NO_MATCH",
    "default_window",
    "empty_explanation",
    "search_log",
    "window_log",
]
