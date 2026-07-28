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
from pathlib import Path

import pytest

from tools import constructs

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


def test_the_construct_matrix_is_current() -> None:
    """Generated from the resolved plan, so a model that gains a construct and
    a table that does not mention it cannot both be committed."""
    page = constructs.PAGE.read_text()
    assert constructs.rendered(page) == page, 'the construct matrix is stale — run `uv run python -m tools.constructs`'


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
_TAUGHT = re.compile(r'^\s*(?:- expression:|where:)\s*\S.*$', re.MULTILINE)


def test_the_guide_teaches_lines_that_exist() -> None:
    """Every expression the guide shows is copied from a model that runs.

    The guide is prose, so nothing else would notice it drifting — and a
    tutorial demonstrating syntax the compiler no longer accepts is worse than
    no tutorial. Only expressions and `where` clauses are checked: the
    dimension blocks are deliberately written in the compact form to be read,
    not to be pasted.
    """
    models = [p.read_text().split('\n') for p in (GUIDE.parent.parent / 'examples').glob('*.yaml')]
    for line in (m.group(0).strip() for m in _TAUGHT.finditer(GUIDE.read_text())):
        assert any(line == other.strip() for model in models for other in model), (
            f'docs/guide.md teaches a line no example model contains:\n  {line}'
        )
