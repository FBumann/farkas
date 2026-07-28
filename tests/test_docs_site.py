"""The one piece of machinery the published site adds, pinned.

``docs/`` is read on GitHub and served as a site, and the sources are written
for the repo. The only thing bridging the two is ``tools.mkdocs_hooks``, which
rewrites links that leave ``docs/`` into blob URLs at build time. It is a
regex over prose — exactly the shape of code that quietly stops matching — so
the rule table is asserted here rather than trusted.

``mkdocs build --strict`` already fails on a dead link *inside* the site, and
CI runs it. What it cannot check is the other side of the rewrite: whether the
repo file a page points at still exists. That is the last test in this module,
and it is the reason to have the file at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.mkdocs_hooks import _rewrite, on_page_markdown

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / 'docs'
REPO_URL = 'https://github.com/FBumann/farkas'
BLOB = f'{REPO_URL}/blob/main'


@pytest.mark.parametrize(
    ('page_dir', 'target', 'expected'),
    [
        # leaves docs/ — becomes a link to the file on GitHub
        ('', '../CONTRIBUTING.md', f'{BLOB}/CONTRIBUTING.md'),
        ('', '../bench/results/latest.jsonl', f'{BLOB}/bench/results/latest.jsonl'),
        ('models', '../../examples/ports/data/transport_pwl.json', f'{BLOB}/examples/ports/data/transport_pwl.json'),
        # a fragment survives the rewrite; it is GitHub's anchor either way
        ('', '../CONTRIBUTING.md#adding-a-ported-model', f'{BLOB}/CONTRIBUTING.md#adding-a-ported-model'),
        # stays inside docs/ — mkdocs resolves it, so the hook must not touch it
        ('', 'SPEC.md', None),
        ('models', '../SPEC.md', None),
        ('models', 'dispatch.md', None),
        ('', 'benchmarks-scaling.html', None),
        ('', 'bench.svg', None),
        # already absolute, or not a path at all
        ('', 'https://github.com/PyPSA/linopy', None),
        ('', '#not-measured-yet', None),
        ('', 'mailto:someone@example.com', None),
    ],
)
def test_rewrite_rule(page_dir: str, target: str, expected: str | None):
    assert _rewrite(target, page_dir, BLOB) == expected


class _File:
    def __init__(self, src_uri: str):
        self.src_uri = src_uri


class _Page:
    def __init__(self, src_uri: str):
        self.file = _File(src_uri)


def _render(markdown: str, src_uri: str = 'ports.md') -> str:
    return on_page_markdown(markdown, page=_Page(src_uri), config={'repo_url': REPO_URL})


def test_rewrites_inline_links_and_leaves_the_rest():
    out = _render('see [contributing](../CONTRIBUTING.md) and [the spec](SPEC.md).')
    assert out == f'see [contributing]({BLOB}/CONTRIBUTING.md) and [the spec](SPEC.md).'


def test_rewrites_a_reference_definition():
    """``[label]: target`` names a destination too — rewriting only inline
    links would leave a whole second spelling behind."""
    assert _render('[ledger]: ../CLAUDE.md\n') == f'[ledger]: {BLOB}/CLAUDE.md\n'


def test_keeps_a_link_title():
    out = _render('[x](../CONTRIBUTING.md "how to help")')
    assert out == f'[x]({BLOB}/CONTRIBUTING.md "how to help")'


def test_leaves_an_image_inside_the_site_alone():
    """An image is a link with a `!` in front, and goes through the same rule.
    `bench.svg` is under `docs/`, so mkdocs copies it and the link stands."""
    assert _render('![the ladder](bench.svg)') == '![the ladder](bench.svg)'


#: `](target)` and `[label]: target`, the two ways a page names a destination.
_TARGETS = re.compile(r'\]\(\s*([^)\s]+)|^\[[^\]]+\]:\s+(\S+)', re.MULTILINE)


def test_every_link_leaving_docs_points_at_a_file_that_exists():
    """The check `mkdocs build --strict` cannot make.

    Inside the site mkdocs validates every link itself. The moment one is
    rewritten to a blob URL it stops being validated by anything — so a page
    can go on pointing at `../bench/results/latest.jsonl` long after the file
    moves, and nothing fails. This is that assertion, and it is why the rewrite
    resolves paths rather than string-matching them.
    """
    broken = []
    for page in sorted(DOCS.rglob('*.md')):
        page_dir = page.parent.relative_to(DOCS).as_posix()  # '.' at the top, which normpath eats
        for inline, reference in _TARGETS.findall(page.read_text()):
            url = _rewrite(inline or reference, page_dir, BLOB)
            if url is None:
                continue  # inside the site; mkdocs checks it
            relative = url.removeprefix(f'{BLOB}/').partition('#')[0]
            if not (REPO / relative).exists():
                broken.append(f'{page.relative_to(REPO)} -> {relative}')
    assert not broken, f'links to repo files that no longer exist: {broken}'
