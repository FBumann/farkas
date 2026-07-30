"""The gallery says what the repo actually contains.

A docs page that shows a model is a **copy** of that model, and a copy rots
unless something asserts it. Three things are checked, each a different way for
the page to become a lie:

- a model exists with no page, so the gallery quietly under-sells the language;
- a page shows YAML that no longer matches the file CI runs;
- a page shows a reference implementation that no longer matches the script;
- the construct matrix says a model exercises something it does not.

The same trade ``linopy/semantics.py`` already makes: copying is fine when a
test asserts it, and rots when nothing does.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterator

import pytest

from tools import constructs, gallery_math

GALLERY = Path(__file__).resolve().parent.parent / 'docs' / 'models'


@pytest.fixture(params=constructs.models(), ids=lambda m: m[0])
def model(request: pytest.FixtureRequest) -> tuple[str, Path]:
    return request.param


def test_every_model_has_a_page(model: tuple[str, Path]) -> None:
    name, _ = model
    assert (GALLERY / f'{name}.md').exists(), (
        f'{name} has no gallery page. A model with no page is invisible to a reader '
        f'deciding whether the language can say theirs.'
    )


def test_the_page_shows_the_model_that_runs(model: tuple[str, Path]) -> None:
    """A YAML fence on the page equals the model file, byte for byte."""
    name, path = model
    fences = re.findall(r'^```yaml\n(.*?)^```', (GALLERY / f'{name}.md').read_text(), re.MULTILINE | re.DOTALL)
    assert path.read_text().rstrip() + '\n' in fences, f'docs/models/{name}.md has drifted from {path}'


def test_no_page_without_a_model() -> None:
    """The reverse: a page for a model that was deleted or renamed."""
    named = {name for name, _ in constructs.models()} | {'index'}
    orphans = sorted(p.stem for p in GALLERY.glob('*.md') if p.stem not in named)
    assert not orphans, f'gallery pages with no model behind them: {orphans}'


def test_the_gallery_math_is_current() -> None:
    """Each page's math block equals what the model produces.

    The fourth way a page becomes a lie, and the one that had already
    happened: three pages stated math their model does not build. A summary
    can be loose — it is prose, and it is meant to be read at a glance — but
    the exact statement beside it has to be exact, and only a generator keeps
    it that way.
    """
    assert gallery_math.main(['--check']) == 0, 'stale gallery math'


def test_every_page_with_a_model_carries_a_math_block() -> None:
    """A page added without the markers would silently opt out of the check
    above, which is the failure mode the check exists to prevent."""
    missing = [
        name for name, _ in constructs.models() if gallery_math.BEGIN not in (GALLERY / f'{name}.md').read_text()
    ]
    assert not missing, (
        f'gallery pages with no math block: {missing} — add the '
        f'{gallery_math.BEGIN}/{gallery_math.END} markers under "## The model"'
    )


def test_every_math_block_opts_into_markdown_inside_html() -> None:
    """`<details>` without `markdown="1"` renders its contents as literal text
    on the site, and the strict build does not notice — literal text is valid.

    The two renderers disagree here and only one of them complains. GitHub
    processes Markdown inside `<details>` regardless and drops the unknown
    attribute; mkdocs has `md_in_html`, which needs it. So the attribute is
    free on one side and load-bearing on the other, which is exactly the kind
    of thing that ships broken.
    """
    for name, _ in constructs.models():
        page = (GALLERY / f'{name}.md').read_text()
        assert '<details markdown="1">' in page, (
            f'docs/models/{name}.md has a math block whose <details> does not carry '
            f'markdown="1" — its tables and $$ blocks will be literal text on the site'
        )


def test_the_generated_evidence_tables_are_current() -> None:
    """Both of the gallery's tables, which is the whole point of generating them.

    The construct matrix comes from the resolved plan, so a model that gains a
    construct and a table that does not mention it cannot both be committed.
    The reference table comes from ``references.json``, the same file
    ``test_ports.py`` asserts against — so the *published* optimum and the
    *asserted* one cannot disagree. They used to be able to: the table was
    hand-written, and hand-written twice, once here and once in the old
    ``docs/ports.md``.
    """
    page = constructs.PAGE.read_text()
    assert constructs.rendered(page) == page, 'the gallery tables are stale — run `uv run python -m tools.constructs`'


PORTS = Path(__file__).resolve().parent.parent / 'examples' / 'ports' / 'references'


@pytest.fixture(params=sorted(PORTS.glob('*.py')), ids=lambda p: p.stem)
def reference(request: pytest.FixtureRequest) -> Path:
    return request.param


def test_the_page_shows_the_reference_that_runs(reference: Path) -> None:
    """The side-by-side embeds a reference script, and a comparison about
    readability has to show code that still exists in that form.

    Caught its own first regression: `ruff format` reflowed a `pivot` chain in
    `transport_dantzig.py` after the page had copied it, and nothing else would
    have noticed. The PEP 723 header is excluded — it is provenance, and the
    comparison is about the modelling.
    """
    page = GALLERY / f'{reference.stem}.md'
    script = reference.read_text()
    body = script[script.index('from __future__') :].rstrip() + '\n'
    fences = re.findall(r'^```python\n(.*?)^```', page.read_text(), re.MULTILINE | re.DOTALL)
    assert body in fences, f'{page} has drifted from {reference}'


GUIDE = Path(__file__).resolve().parent.parent / 'docs' / 'guide.md'
_TAUGHT_START = re.compile(r'^([ \t]*)(?:- expression:|where:)\s*\S')
_QUOTED = ('expression', 'where')


def _normalise(text: str) -> str:
    """One expression, whitespace-flattened, so folding is not a difference.

    A YAML folded scalar joins its lines with single spaces, so the same math
    written on one line and over five parses to the same string only after the
    runs of whitespace collapse. Comparing *parsed* values rather than source
    lines is what lets the guide and the models each wrap where they read best.
    """
    return ' '.join(text.split())


def _taught(markdown: str) -> list[str]:
    """Every expression and ``where`` the guide shows, as parsed strings.

    A snippet is its opening line plus the lines indented under it, so a folded
    scalar is collected whole rather than truncated to its ``>-`` header.
    """
    lines = markdown.split('\n')
    snippets: list[str] = []
    i = 0
    while i < len(lines):
        start = _TAUGHT_START.match(lines[i])
        if start is None:
            i += 1
            continue
        indent, block = len(start.group(1)), [lines[i]]
        i += 1
        while i < len(lines) and lines[i].strip() and _indent_of(lines[i]) > indent:
            block.append(lines[i])
            i += 1
        parsed = yaml.safe_load(textwrap.dedent('\n'.join(block)))
        entry = parsed[0] if isinstance(parsed, list) else parsed
        snippets.extend(_normalise(v) for k, v in entry.items() if k in _QUOTED)
    return snippets


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _declared(node: object) -> Iterator[str]:
    """Every ``expression`` and ``where`` string anywhere in a model file."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _QUOTED and isinstance(value, str):
                yield _normalise(value)
            yield from _declared(value)
    elif isinstance(node, list):
        for item in node:
            yield from _declared(item)


def test_the_guide_teaches_lines_that_exist() -> None:
    """Every expression the guide shows is copied from a model that runs.

    The guide is prose, so nothing else would notice it drifting — and a
    tutorial demonstrating syntax the compiler no longer accepts is worse than
    no tutorial. Only expressions and `where` clauses are checked: the
    dimension blocks are deliberately written in the compact form to be read,
    not to be pasted.

    Compared as **parsed values**, not as source lines. The line form was
    equivalent only while every expression fitted on one line; once a model
    folds one across several, a line-wise check silently degrades to comparing
    ``- expression: >-`` against itself and asserts nothing about the math.

    The corpus is ``constructs.models()`` — the same list the gallery and the
    matrix are built from — rather than a glob of ``examples/*.yaml``, which
    silently excluded the two ports one directory down. A guide line taken
    from a port would have failed here for not existing.
    """
    declared = {text for _, path in constructs.models() for text in _declared(yaml.safe_load(path.read_text()))}
    taught = _taught(GUIDE.read_text())
    assert taught, 'no expressions found in docs/guide.md — the extractor is broken'
    for expression in taught:
        assert expression in declared, f'docs/guide.md teaches an expression no example model contains:\n  {expression}'
