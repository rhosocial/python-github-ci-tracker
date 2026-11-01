# tests/app/test_documentation.py
"""The docs tree is navigable: every page is listed, both languages match, no dead links.

A page that exists but is not in any table of contents is invisible: readers
follow the list, and a search engine is the only other way in. That happened once
(`docs/*/value.md` was added to SUMMARY.md but not to the language README), which
is the reason these three checks exist rather than a note in a contributing doc.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
LANGS = ("en_US", "zh_CN")

#: The page a reader lands on, and the mirror some static-site tools generate.
LANDING = "README.md"
MIRROR = "SUMMARY.md"

LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _pages(lang: str) -> set[str]:
    """Every chapter of a language, i.e. its pages minus its two indexes."""
    return {page.name for page in (DOCS / lang).glob("*.md")} - {LANDING, MIRROR}


def _chapters(lang: str, index: str) -> set[str]:
    """The chapters one index of that language links to.

    Links that leave the directory (the root READMEs) and the index's own name
    are navigation, not chapter entries -- comparing them across two indexes
    would report a difference that means nothing.
    """
    path = DOCS / lang / index
    linked: set[str] = set()
    for _, target in LINK.findall(path.read_text(encoding="utf-8")):
        if target.startswith(("http", "#", "mailto:")):
            continue
        target = target.split("#", 1)[0].strip()
        if not target.endswith(".md"):
            continue
        name = Path(target).name
        if name in _pages(lang) or name in {LANDING, MIRROR}:
            linked.add(name)
    return linked


@pytest.mark.parametrize("lang", LANGS)
def test_every_page_is_listed_on_the_landing_page(lang: str) -> None:
    """A chapter the landing page does not list is invisible to a reader.

    `value.md` was added to SUMMARY.md only, and a reader opening the language
    README could not find it -- which is the whole point of a table of contents.
    """
    orphans = _pages(lang) - _chapters(lang, LANDING)
    assert orphans == set(), f"docs/{lang}/{LANDING} does not list: {sorted(orphans)}"


def test_the_summary_mirrors_the_landing_page() -> None:
    """Two indexes that disagree are one index too many."""
    for lang in LANGS:
        listed = _chapters(lang, LANDING)
        mirrored = _chapters(lang, MIRROR)
        difference = listed ^ mirrored
    assert mirrored == listed, f"docs/{lang}/{MIRROR} differs from {LANDING}: {sorted(difference)}"


def test_both_languages_carry_the_same_pages() -> None:
    """A page in one language only reads as an abandoned translation."""
    only = _pages("en_US") ^ _pages("zh_CN")
    assert only == set(), f"page present in one language only: {sorted(only)}"


@pytest.mark.parametrize("lang", LANGS)
def test_no_link_points_nowhere(lang: str) -> None:
    """A relative link that does not resolve is a dead end for the reader."""
    broken: list[str] = []
    for page in sorted((DOCS / lang).glob("*.md")):
        for label, target in LINK.findall(page.read_text(encoding="utf-8")):
            if target.startswith(("http", "#", "mailto:")):
                continue
            if not (page.parent / target.split("#", 1)[0]).resolve().exists():
                broken.append(f"{page.name}: [{label}]({target})")
    assert broken == [], "broken links: " + "; ".join(broken)
