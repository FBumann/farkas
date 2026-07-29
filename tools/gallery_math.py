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
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from farkas.typeset import to_markdown
from tools.constructs import models

ROOT = Path(__file__).resolve().parent.parent
GALLERY = ROOT / 'docs' / 'models'
SYMBOLS = ROOT / 'examples' / 'symbols'
BEGIN, END = '<!-- math:begin -->', '<!-- math:end -->'


def _block(name: str, path: Path) -> str:
    """The generated section for one model: a disclosure holding its math."""
    table = SYMBOLS / f'{name}.yaml'
    math = to_markdown(path, symbols=table if table.exists() else None, legend=True)
    return f'<details markdown="1">\n<summary>The same model, as math</summary>\n\n{math}\n</details>'


def rendered(page: str, name: str, path: Path) -> str:
    """*page* with the block between the markers replaced."""
    i, j = page.index(BEGIN) + len(BEGIN), page.index(END)
    return page[:i] + '\n' + _block(name, path) + '\n' + page[j:]


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

    stale = []
    for name, path, page_path in pages():
        page = page_path.read_text()
        updated = rendered(page, name, path)
        if opts.check:
            if updated != page:
                stale.append(name)
        elif updated != page:
            page_path.write_text(updated)

    if opts.check:
        if stale:
            print(
                f'stale math on {len(stale)} gallery page(s): {", ".join(stale)}\n'
                f'run `uv run python -m tools.gallery_math`',
                file=sys.stderr,
            )
            return 1
        print(f'{len(pages())} gallery pages match their models')
        return 0
    print(f'{len(pages())} gallery pages refreshed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
