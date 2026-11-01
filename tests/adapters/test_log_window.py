# tests/adapters/test_log_window.py
"""Bounded reading of a log: no content assumptions, only addressing.

Every test here is about *which lines* come back, never about what the lines
mean. The service is a general CI tracker: it cannot know that a project prints
pytest summaries, `##[error]` markers or a runner teardown, and a default window
that guessed at those would show the wrong lines for every other project.

So the rules are the caller's: `tail`, `from_line`/`to_line`, `grep`, or nothing
-- and nothing means the end of the document, bounded.
"""

from __future__ import annotations

from rhosocial.github_ci_tracker.adapters.mcp.logs import (
    DEFAULT_TAIL,
    EMPTY_GREP,
    EMPTY_LOG,
    EMPTY_TAIL,
    EMPTY_WINDOW,
    MAX_CHARS,
    empty_explanation,
    search_log,
    window_log,
)


def _lines(count: int) -> str:
    return "\n".join(f"line {number}" for number in range(1, count + 1)) + "\n"


class TestTheDefaultWindow:
    def test_the_default_is_the_end_of_the_document(self) -> None:
        window = window_log(_lines(500))

        assert window.total_lines == 500
        assert window.last_line == 500
        assert window.first_line == 500 - DEFAULT_TAIL + 1
        assert window.returned_lines == DEFAULT_TAIL
        assert window.truncated is True

    def test_a_short_document_comes_back_whole(self) -> None:
        window = window_log("one\ntwo\n")

        assert window.content == "one\ntwo"
        assert window.returned_lines == 2
        assert window.truncated is False

    def test_an_empty_document_says_so(self) -> None:
        window = window_log("")

        assert window.content == ""
        assert window.empty_reason == EMPTY_LOG


class TestExplicitSelections:
    def test_a_tail_is_honoured(self) -> None:
        window = window_log(_lines(10), tail=3)

        assert window.content.splitlines() == ["line 8", "line 9", "line 10"]
        assert window.first_line == 8
        assert window.matches is None

    def test_a_line_range_is_honoured(self) -> None:
        window = window_log(_lines(10), from_line=2, to_line=4)

        assert window.content.splitlines() == ["line 2", "line 3", "line 4"]
        assert (window.first_line, window.last_line) == (2, 4)

    def test_grep_keeps_only_matching_lines_with_context(self) -> None:
        content = "a\nboom\nb\nc\n"
        window = window_log(content, grep="boom", context=1)

        assert window.matches == [2]
        assert window.content.splitlines() == ["a", "boom", "b"]

    def test_a_pattern_that_is_not_valid_regex_falls_back_to_literal(self) -> None:
        window = window_log("x\n[a\n", grep="[a", context=0)

        assert window.matches == [2]


class TestEmptyReasons:
    def test_a_non_positive_tail_is_rejected(self) -> None:
        window = window_log(_lines(5), tail=0)

        assert window.empty_reason == EMPTY_TAIL
        assert window.truncated is True

    def test_a_range_outside_the_document_is_reported(self) -> None:
        window = window_log(_lines(5), from_line=50, to_line=60)

        assert window.empty_reason == EMPTY_WINDOW

    def test_a_pattern_with_no_match_is_not_an_empty_log(self) -> None:
        window = window_log(_lines(5), grep="nope")

        assert window.empty_reason == EMPTY_GREP
        assert window.total_lines == 5

    def test_each_reason_has_an_explanation(self) -> None:
        assert "no log text" in empty_explanation(EMPTY_LOG, total_lines=0, grep=None)
        assert "positive" in empty_explanation(EMPTY_TAIL, total_lines=5, grep=None)
        assert "outside" in empty_explanation(EMPTY_WINDOW, total_lines=5, grep=None)
        assert "No line matched" in empty_explanation(EMPTY_GREP, total_lines=5, grep="x")


class TestTheCharacterCeiling:
    def test_a_tail_keeps_its_end_when_the_ceiling_is_hit(self) -> None:
        content = "\n".join(f"line {number} " + "x" * 100 for number in range(1, 2001))
        window = window_log(content, tail=2000, max_chars=MAX_CHARS)

        assert len(window.content) <= MAX_CHARS
        assert window.last_line == 2000, "the end of a tail is what a tail is for"
        assert window.truncated is True

    def test_an_explicit_range_keeps_its_beginning(self) -> None:
        content = "\n".join(f"line {number} " + "x" * 100 for number in range(1, 2001))
        window = window_log(content, from_line=1, to_line=2000, max_chars=MAX_CHARS)

        assert len(window.content) <= MAX_CHARS
        assert window.first_line == 1, "an explicit range starts where it was told"


class TestSearch:
    def test_every_match_position_is_reported(self) -> None:
        found = search_log("a\nboom\nb\nboom\n", "boom", context=0)

        assert found.matches == [2, 4]
        assert found.total_matches == 2

    def test_no_match_is_a_reason_not_an_empty_log(self) -> None:
        found = search_log("a\nb\n", "boom")

        assert found.empty_reason == "no_match"
        assert found.total_lines == 2

    def test_an_empty_document_is_reported_as_such(self) -> None:
        found = search_log("", "boom")

        assert found.empty_reason == "log_empty"
