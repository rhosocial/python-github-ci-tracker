# tests/core/test_build.py
"""The build stamp: absent is absent, never a guess.

``__version__`` is a source-tree constant, so every image of every commit
answers the same thing and a deployment cannot tell whether the process it is
talking to was built from the commit just pushed. The image stamps the commit,
the CI build number and the build time into the environment instead, and these
tests pin how that stamp is read -- including the case where there is none,
which must stay ``None`` rather than fall back to something that only looks like
an answer.
"""

from __future__ import annotations

from rhosocial.github_ci_tracker.build import (
    BUILT_AT_ENV,
    NUMBER_ENV,
    REVISION_ENV,
    BuildInfo,
    build_info,
)

SHA = "e5815a5f5b31df495dc69638d11602df685a0e4b"


class TestReadingTheStamp:
    def test_a_stamped_build_is_read_whole(self) -> None:
        info = BuildInfo.from_env(
            {
                REVISION_ENV: SHA,
                NUMBER_ENV: "42",
                BUILT_AT_ENV: "2026-10-31T02:27:51Z",
            }
        )

        assert info.revision == SHA
        assert info.short_revision == "e5815a5"
        assert info.number == "42"
        assert info.built_at == "2026-10-31T02:27:51Z"
        assert info.known is True

    def test_an_unstamped_build_reports_nothing(self) -> None:
        info = BuildInfo.from_env({})

        assert info.known is False
        assert info.as_dict() == {
            "revision": None,
            "short_revision": None,
            "number": None,
            "built_at": None,
        }

    def test_blank_values_are_not_a_stamp(self) -> None:
        """An empty build argument must not become a revision of two spaces."""
        info = BuildInfo.from_env({REVISION_ENV: "  ", NUMBER_ENV: ""})

        assert info.revision is None
        assert info.number is None
        assert info.known is False

    def test_the_label_survives_a_missing_half(self) -> None:
        assert BuildInfo(revision=SHA).label == "e5815a5"
        assert BuildInfo(number="7").label == "7"
        assert BuildInfo().label is None

    def test_the_process_environment_is_the_default_source(self, monkeypatch) -> None:
        monkeypatch.setenv(REVISION_ENV, SHA)

        assert build_info().revision == SHA
