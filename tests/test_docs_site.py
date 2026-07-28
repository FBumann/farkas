"""The docs are read in two places; these are the checks that keep them honest in both.

``docs/`` is browsed on GitHub and served as a site, from one set of files. A
link *inside* ``docs/`` is relative and mkdocs validates it — ``build --strict``
in CI fails on a dead one. A link *outside* ``docs/`` cannot be relative,
because the site has no `../CONTRIBUTING.md` to resolve to, so it is written as
a full GitHub URL.

That convention is the whole mechanism, and it is unenforceable by mkdocs in
both directions: a relative link escaping ``docs/`` builds a silent 404, and a
blob URL is opaque to every checker there is — the file it names can be deleted
and nothing anywhere fails. Hence this module.

``docs/README.md`` is the one exception and is exempted throughout: it is
excluded from the site (``exclude_docs``), exists only as the folder view
GitHub renders, and its relative links out of the tree are correct there.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / 'docs'
REPO_URL = 'https://github.com/FBumann/farkas'
BLOB = f'{REPO_URL}/blob/main'

#: `](target)` and `[label]: target`, the two ways markdown names a destination.
_TARGETS = re.compile(r'\]\(\s*([^)\s]+)|^\[[^\]]+\]:\s+(\S+)', re.MULTILINE)

#: Already absolute, a bare fragment, or a protocol that names no path.
_ABSOLUTE = re.compile(r'^([a-z][a-z0-9+.-]*:|//|#|/)', re.IGNORECASE)


def _pages() -> list[Path]:
    """Every page mkdocs builds — so, not `docs/README.md`."""
    return [p for p in sorted(DOCS.rglob('*.md')) if p.relative_to(DOCS).as_posix() != 'README.md']


def _targets(page: Path) -> list[str]:
    return [inline or reference for inline, reference in _TARGETS.findall(page.read_text())]


def test_no_relative_link_escapes_the_docs_tree():
    """The failure mkdocs cannot see.

    `[x](../CONTRIBUTING.md)` is correct in the repo and a 404 on the site.
    mkdocs resolves it against `docs/`, finds nothing above the root, and —
    because the target is outside the tree it knows about — does not treat it
    as a broken internal link. It just ships. Write the full GitHub URL.
    """
    escaping = []
    for page in _pages():
        for target in _targets(page):
            if _ABSOLUTE.match(target):
                continue
            path = target.partition('#')[0]
            if not path:
                continue
            resolved = (page.parent / path).resolve()
            if resolved != DOCS and DOCS not in resolved.parents:
                escaping.append(f'{page.relative_to(REPO)} -> {target}')
    assert not escaping, (
        f'relative links pointing outside docs/, which 404 on the site: {escaping}\nwrite them as {BLOB}/<path> instead'
    )


def test_every_blob_url_names_a_file_that_exists():
    """The other half: a blob URL is checked by nothing at all.

    mkdocs treats it as external and never follows it; the repo has no reason
    to notice it. So a page can go on pointing at `bench/results/latest.jsonl`
    long after the file moves, and the first report is a reader hitting
    GitHub's 404.
    """
    broken = []
    for page in [*_pages(), DOCS / 'README.md']:
        for target in _targets(page):
            if not target.startswith(BLOB):
                continue
            relative = target.removeprefix(f'{BLOB}/').partition('#')[0]
            if not (REPO / relative).exists():
                broken.append(f'{page.relative_to(REPO)} -> {relative}')
    assert not broken, f'links to repo files that no longer exist: {broken}'


def test_links_to_our_own_files_are_all_spelled_the_same_way():
    """One spelling, so the check above cannot be dodged.

    A link at a file in this repo written any other way — `tree/`, `raw/`, a
    permalinked sha, a branch that will vanish — reaches the right page today
    and is skipped by the existence check, which only recognises `blob/main`.
    Issue and PR links are not file links and are left alone.
    """
    file_shaped = re.compile(rf'^{re.escape(REPO_URL)}/(blob|tree|raw|blame)/')
    stray = [
        f'{page.relative_to(REPO)} -> {target}'
        for page in [*_pages(), DOCS / 'README.md']
        for target in _targets(page)
        if file_shaped.match(target) and not target.startswith(f'{BLOB}/')
    ]
    assert not stray, f'links at repo files not written as {BLOB}/<path>: {stray}'


def test_the_convention_is_actually_in_use():
    """A guard on the guards.

    Every assertion above passes vacuously on a docs tree with no outbound
    links at all — including one where a bad refactor stripped them. Pin that
    the arrangement they describe exists.
    """
    urls = [t for page in _pages() for t in _targets(page) if t.startswith(BLOB)]
    assert len(urls) >= 15, f'expected the docs to link out to the repo; found {len(urls)}'
