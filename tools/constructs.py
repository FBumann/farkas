"""The gallery's two evidence tables: what each model exercises, and what
somebody else says its answer is.

    uv run python -m tools.constructs           # rewrite both tables
    uv run python -m tools.constructs --check   # fail if either has drifted

The point of generating them is that a hand-kept evidence table is the exact
shape of claim that rots: it is written once when it is true, and nothing
fails when a model changes underneath it. ``tests/test_models_gallery.py``
asserts the committed tables equal what this produces.

**Constructs** are read off the **logical plan**, not the YAML text. Grepping
for ``roll(`` would count a construct inside a macro that never expands, miss
one a macro introduces, and disagree with itself about whether a bound written
as ``0`` is a bound. ``lower_program`` needs no data, so the plan is available
for any model in the repo — and it is what the engine actually builds.

**References** are read off ``examples/ports/references.json``, which is the
same file ``tests/test_ports.py`` asserts against. That is the whole point: a
published optimum and an asserted optimum that can disagree is a correctness
claim with nothing behind it. Adding a port is a JSON entry and a regenerate.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

from farkas.api import load_schema
from farkas.lowering import lower_program
from farkas.relational import plan

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / 'docs' / 'models' / 'index.md'
REFERENCES = json.loads((ROOT / 'examples' / 'ports' / 'references.json').read_text())
BEGIN, END = '<!-- constructs:begin -->', '<!-- constructs:end -->'
REF_BEGIN, REF_END = '<!-- references:begin -->', '<!-- references:end -->'

#: Column order is the order a reader meets these in docs/SPEC.md, not alphabetical
#: and not by how many models happen to use them.
COLUMNS = ('sum', 'group_sum', 'roll / shift', 'where', 'bounds', 'piecewise', 'MILP')


def walk(node: Any) -> Iterator[Any]:
    """Every dataclass node reachable from *node*, itself included.

    Structural rather than a visitor with a case per type: a new expression
    node then shows up in this table by existing, instead of by someone
    remembering to add it here.
    """
    if is_dataclass(node) and not isinstance(node, type):
        yield node
        for f in fields(node):
            yield from walk(getattr(node, f.name))
    elif isinstance(node, tuple | list):
        for item in node:
            yield from walk(item)


def constructs(model: Path) -> set[str]:
    """The set of columns *model* exercises."""
    schema = load_schema(model)
    program = lower_program(schema)
    nodes = list(walk(program))
    used: set[str] = set()

    for node in nodes:
        if isinstance(node, plan.Sum):
            used.add('sum')
        elif isinstance(node, plan.GroupSum):
            used.add('group_sum')
        elif isinstance(node, plan.Translate):
            used.add('roll / shift')

    if any(isinstance(n, plan.Predicate) for n in nodes):
        used.add('where')
    if any(v.variable_type != 'continuous' for v in program.variables):
        used.add('MILP')
    # A bound is a *declared* one only if it is not the open default. Reading
    # the plan rather than the YAML means `lower: 0` and an omitted lower are
    # distinguishable, which is the whole reason for not grepping.
    if any(_bounded(v) for v in program.variables):
        used.add('bounds')
    # `piecewise:` lowers away into a lambda formulation, so by the time the
    # plan exists there is nothing left to recognise: the surface declaration
    # is the only evidence it was ever there.
    if getattr(schema, 'piecewise', None):
        used.add('piecewise')
    return used


def _bounded(v: plan.VariableDeclaration) -> bool:
    open_at = {float('-inf'): 'lower', float('inf'): 'upper'}
    for side in ('lower', 'upper'):
        bound = getattr(v, side)
        if not (isinstance(bound, plan.Constant) and open_at.get(bound.value) == side):
            return True
    return False


def table(models: list[tuple[str, Path]]) -> str:
    """Markdown, one row per model, `·` where a construct is absent.

    A dot rather than an empty cell: an empty one reads as "not checked", and
    the holes in this table are the informative part.
    """
    lines = [
        '| model | verified | ' + ' | '.join(f'`{c}`' if c != 'MILP' else c for c in COLUMNS) + ' |',
        '|---' * (len(COLUMNS) + 2) + '|',
    ]
    for name, path in models:
        used = constructs(path)
        cells = ['**✓**' if c in used else '·' for c in COLUMNS]
        # the badge is *external* verification, not "there is a test": every
        # model here is exercised by the suite, and only these two are checked
        # against a number that did not come from us
        badge = f'**✔** {REFERENCES[name]["objective"]:g}' if name in REFERENCES else '·'
        lines.append(f'| [{name}]({name}.md) | {badge} | ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines)


def references_table() -> str:
    """One row per verified port, straight from ``references.json``.

    The optimum is written as ``repr`` rather than rounded: this is the number
    the assertion uses, and a table that rounds it is a different claim from
    the one the test makes.

    ``rtol`` is a column rather than a footnote even though every port shares
    one today — a footnote saying "all matched to 1e-09" becomes quietly false
    the first time one does not, and nothing would catch it.
    """
    lines = [
        '| port | optimum | `rtol` | duals | reference |',
        '|---|---|---|---|---|',
    ]
    notes = []
    for name, entry in sorted(REFERENCES.items()):
        duals = '**✔**' if entry.get('duals') else '·'
        mark = f'[^{name}]' if entry.get('corroborated_by') else ''
        lines.append(
            f'| [{name}]({name}.md) | {entry["objective"]!r} | {entry["rtol"]:g} | '
            f'{duals} | {entry["provenance"]}{mark} |'
        )
        if corroborated := entry.get('corroborated_by'):
            notes.append(f'[^{name}]: {corroborated}')
    # Corroboration runs to a paragraph, so it goes under the table rather than
    # in a cell — `footnotes` is already on in mkdocs.yml and renders in the
    # repo view as plain text that still reads.
    return '\n'.join(lines) + ('\n\n' + '\n\n'.join(notes) if notes else '')


def models() -> list[tuple[str, Path]]:
    """Every model the gallery shows, examples before ports."""
    examples = sorted((ROOT / 'examples').glob('*.yaml'))
    ports = sorted((ROOT / 'examples' / 'ports').glob('*.yaml'))
    return [(p.stem, p) for p in examples] + [(p.stem, p) for p in ports]


def _replace(page: str, begin: str, end: str, body: str) -> str:
    i, j = page.index(begin) + len(begin), page.index(end)
    return page[:i] + '\n' + body + '\n' + page[j:]


def rendered(page: str) -> str:
    """*page* with both generated blocks replaced."""
    page = _replace(page, BEGIN, END, table(models()))
    return _replace(page, REF_BEGIN, REF_END, references_table())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true', help='fail if the committed table has drifted')
    opts = ap.parse_args(argv)

    page = PAGE.read_text()
    updated = rendered(page)
    if opts.check:
        if updated != page:
            print(f'{PAGE} is stale — run `uv run python -m tools.constructs`', file=sys.stderr)
            return 1
        print(f'{PAGE} matches the models')
        return 0
    PAGE.write_text(updated)
    print(f'{PAGE} refreshed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
