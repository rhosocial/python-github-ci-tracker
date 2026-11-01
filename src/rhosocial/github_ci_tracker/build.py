# src/rhosocial/github_ci_tracker/build.py
"""Which build is answering: the commit, the build number, the build time.

``__version__`` is a constant in the source tree, so every image ever published
answers the same thing for as long as the version is not bumped, and a
deployment cannot tell "am I running the commit I pushed ten minutes ago?" from
it. The image build stamps three facts into the environment instead (see
``Dockerfile`` and ``.github/workflows/images.yml``), and this module reads them
back for the probes and for ``ci_status``.

Nothing here is required. A checkout running under ``pytest``, or an image built
by hand without the build arguments, reports ``None`` for every field, and every
surface shows the null rather than inventing a value -- "not stamped" is an
answer, and a fabricated one is not.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

#: The commit the image was built from, as passed by the publishing workflow.
REVISION_ENV = "CITRACKER_BUILD_REVISION"
#: The CI build number (the publishing workflow's run number).
NUMBER_ENV = "CITRACKER_BUILD_NUMBER"
#: When the image was built, ISO 8601 UTC.
BUILT_AT_ENV = "CITRACKER_BUILD_BUILT_AT"


def _clean(value: str | None) -> str | None:
    """Empty and whitespace-only values are not a build stamp."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


@dataclass(frozen=True)
class BuildInfo:
    """What the build said about itself, when it said anything at all."""

    #: Full commit sha, e.g. ``e5815a5f5b31df495dc69638d11602df685a0e4b``.
    revision: str | None = None
    #: The publishing workflow's run number; monotonic per workflow.
    number: str | None = None
    #: Build timestamp, ISO 8601 with an explicit offset.
    built_at: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> BuildInfo:
        """Read the stamp from ``environ`` (the process environment by default)."""
        source = os.environ if environ is None else environ
        return cls(
            revision=_clean(source.get(REVISION_ENV)),
            number=_clean(source.get(NUMBER_ENV)),
            built_at=_clean(source.get(BUILT_AT_ENV)),
        )

    @property
    def short_revision(self) -> str | None:
        """The revision as it appears in ``git log --oneline``."""
        return self.revision[:7] if self.revision else None

    @property
    def label(self) -> str | None:
        """``<number>-<short sha>``, whichever halves exist, or ``None``."""
        parts = [part for part in (self.number, self.short_revision) if part]
        return "-".join(parts) or None

    @property
    def known(self) -> bool:
        """Whether the build said anything about itself."""
        return self.revision is not None or self.number is not None or self.built_at is not None

    def as_dict(self) -> dict[str, str | None]:
        """The wire form shared by the probes and ``ci_status``."""
        return {
            "revision": self.revision,
            "short_revision": self.short_revision,
            "number": self.number,
            "built_at": self.built_at,
        }


def build_info() -> BuildInfo:
    """The running build.

    Read on each call rather than cached at import: the environment is the only
    source, reading it is free, and a cached value would make the field
    untestable exactly where it matters (the probes).
    """
    return BuildInfo.from_env()


__all__ = [
    "BUILT_AT_ENV",
    "NUMBER_ENV",
    "REVISION_ENV",
    "BuildInfo",
    "build_info",
]
