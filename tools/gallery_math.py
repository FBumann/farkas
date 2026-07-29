"""Each gallery page's math, generated from the model the page shows.

    uv run python -m tools.gallery_math           # rewrite every page's math block
    uv run python -m tools.gallery_math --check   # fail if any has drifted

A page that states its model's math by hand is the same shape of claim as a
hand-kept coverage table: written once when it was true, with nothing failing
when the model changes underneath it. That is not hypothetical here — three
pages had already drifted when this was written, and the drift was in the
direction that matters, the page claiming *less* constraint than the model
builds:

- ``dispatch`` displayed a bound for every ``(s, g)`` while the model masks
  with ``where: "p_max > 0"`` — the very line the prose underneath calls the
  one worth pausing on;
- ``storage`` wrote ``\\eta\\,\\mathrm{charge}_s`` for a parameter the model
  does not have, having hardcoded ``0.9``;
- ``transport`` wrote ``\\sum_{g \\in \\mathrm{bus}}``, which is not
  well-formed — ``bus`` is a coordinate map, not a set.

The hand-written one-liner stays: it is a *summary*, and a good one, doing a
job the generated block does not. What is generated is the exact statement
underneath it, which is the thing that has to be true.

Notation comes from ``examples/symbols/<model>.yaml`` where one exists, so a
page keeps the symbols its prose already uses; models without a table get the
derived symbols, which are plain but never ambiguous.

The toggle is ``<details markdown="1">``, and every part of that is load
bearing, because these pages are read in two renderers.

On **GitHub**: the sanitiser strips ``<style>``, ``class``, ``onclick`` and a
bare ``<input>``, so the CSS-only tab trick cannot survive it — ``<details>``
and math inside it both do. Unknown attributes are dropped, so ``markdown="1"``
costs nothing there.

On the **site**: ``md_in_html`` is enabled, and without ``markdown="1"`` it
treats everything inside the element as raw HTML — the tables and the ``$$``
blocks render as literal text. The strict build does not catch that, because
literal text is valid; ``tests/test_docs_site.py`` does.

``pymdownx.tabbed`` is enabled, so real tabs are now available — but a
``=== "Math"`` marker is literal text on GitHub, and mkdocs.yml is explicit
that these pages are meant to render in both places. If that ever stops being
true, tabs are a change to :func:`_block` and to nothing else, which is the
reason to generate this rather than hand-write it even once.

``docs/index.md`` is the exception, and gets :func:`_home_block`: it is the one
page under ``docs/`` that is *only* ever the site — ``README.md`` is what
GitHub renders for the repo — so its block does use tabs, and shows the LaTeX
source beside the math it sets. Same model, same generation, different marker
(``home-math:``) because it is a different rendering of it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from farkas.typeset import to_latex, to_markdown
from tools.constructs import models

ROOT = Path(__file__).resolve().parent.parent
GALLERY = ROOT / 'docs' / 'models'
SYMBOLS = ROOT / 'examples' / 'symbols'
BEGIN, END = '<!-- math:begin -->', '<!-- math:end -->'

#: The home page shows one model end to end, and it is the quickstart's.
HOME = ROOT / 'docs' / 'index.md'
HOME_MODEL = ROOT / 'examples' / 'dispatch.yaml'
HOME_BEGIN, HOME_END = '<!-- home-math:begin -->', '<!-- home-math:end -->'


def _block(name: str, path: Path) -> str:
    """The generated section for one model: a disclosure holding its math."""
    table = SYMBOLS / f'{name}.yaml'
    math = to_markdown(path, symbols=table if table.exists() else None, legend=True)
    return f'<details markdown="1">\n<summary>The same model, as math</summary>\n\n{math}\n</details>'


def _indent(text: str) -> str:
    """Tab content — four spaces, and blank lines stay blank rather than ragged."""
    return '\n'.join(f'    {line}' if line else '' for line in text.splitlines())


def _home_block() -> str:
    """The home page's tabs: the same math set, and the source that sets it.

    Notation comes from the symbol table, so both tabs read in the symbols the
    gallery's dispatch page already uses — and the LaTeX tab is the literal
    string ``fk.to_latex`` returns, not a retyping of it.

    Typst is deliberately absent: :class:`~farkas.typeset.symbols.SymbolTable`
    entries are LaTeX strings, and ``to_typst`` passes them through verbatim,
    so this model prints ``\\mathcal{S}`` into a Typst document. That is a bug
    in the symbol table, not something a docs page should paper over by
    quietly showing derived symbols instead.
    """
    table = SYMBOLS / 'dispatch.yaml'
    options = {'symbols': table, 'legend': False}
    tabs = {
        'The math': to_markdown(HOME_MODEL, **options),
        'LaTeX': f'```latex\n{to_latex(HOME_MODEL, **options).rstrip()}\n```',
        'How': _HOW,
    }
    return '\n'.join(f'=== "{title}"\n\n{_indent(body)}\n' for title, body in tabs.items())


#: The third tab, which is prose rather than generated. `ruff format` reaches
#: into ```python fences in Markdown, so what is written here has to be what
#: ruff would write — two spaces before a trailing comment, not aligned ones,
#: or the generated page fails `ruff format --check` and the fix is here.
_HOW = """```python
import farkas as fk

fk.to_latex('dispatch.yaml')  # amsmath align
fk.to_typst('dispatch.yaml')  # compiles without a TeX toolchain
fk.to_markdown('dispatch.yaml')  # renders as-is on GitHub
```

No data, no solver, no lane: it reads the same validated model both lanes read,
so a `piecewise:` block prints as the formulation it expands to rather than the
sugar it was written as. `--symbols` points at a sidecar table when the derived
symbols are not the ones your paper uses, and `--standalone` emits a document
that compiles.

```bash
python -m farkas latex dispatch.yaml --standalone -o dispatch.tex
```"""


def rendered(page: str, name: str, path: Path) -> str:
    """*page* with the block between the markers replaced."""
    i, j = page.index(BEGIN) + len(BEGIN), page.index(END)
    return page[:i] + '\n' + _block(name, path) + '\n' + page[j:]


def rendered_home(page: str) -> str:
    """``docs/index.md`` with its tabbed block replaced."""
    i, j = page.index(HOME_BEGIN) + len(HOME_BEGIN), page.index(HOME_END)
    return page[:i] + '\n' + _home_block() + page[j:]


def pages() -> list[tuple[str, Path, Path]]:
    """Every (name, model, page) the gallery covers and this tool can fill.

    A page with no markers is skipped rather than an error: adding the block
    to a page is a deliberate edit, and this tool is not the thing that
    decides which pages have one.
    """
    found = []
    for name, path in models():
        page = GALLERY / f'{name}.md'
        if page.exists() and BEGIN in page.read_text():
            found.append((name, path, page))
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true', help='fail if any committed block has drifted')
    opts = ap.parse_args(argv)

    work = [(name, page_path, rendered(page_path.read_text(), name, path)) for name, path, page_path in pages()]
    if HOME_BEGIN in HOME.read_text():
        work.append(('index', HOME, rendered_home(HOME.read_text())))

    stale = []
    for name, page_path, updated in work:
        if updated == page_path.read_text():
            continue
        if opts.check:
            stale.append(name)
        else:
            page_path.write_text(updated)

    if opts.check:
        if stale:
            print(
                f'stale math on {len(stale)} page(s): {", ".join(stale)}\nrun `uv run python -m tools.gallery_math`',
                file=sys.stderr,
            )
            return 1
        print(f'{len(work)} pages match their models')
        return 0
    print(f'{len(work)} pages refreshed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
