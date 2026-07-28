"""The whole architecture, one model, one stage at a time — run it and read.

ARCHITECTURE.md describes the pipeline; this script *executes* it one stage at
a time and prints the artifact each stage produces. Nothing here is a
reimplementation: every call is the same public entry point ``fk.solve`` takes
internally, so what you see is what actually runs.

    uv run python examples/walkthrough.py

Its output is committed as ``examples/walkthrough.out`` and asserted line for
line by ``tests/test_walkthrough.py``, so the narration cannot go stale
unnoticed: a stage that starts saying something else fails CI, and the diff of
the regenerated file is the record of what changed. Everything printed is
therefore deterministic.

The point it is trying to make is the thesis in ARCHITECTURE.md: a YAML math
spec is a closed AST known before any data is touched. Stages 1-3 happen with
no data bound at all; only stage 4 sees a number.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import polars as pl

import farkas as fk
from farkas.expansion import parse_and_expand
from farkas.expression_parser import parse_expression
from farkas.lowering import lower_program
from farkas.relational.executor import PolarsExecutor
from farkas.sources import tidy_sources

HERE = Path(__file__).parent
MODEL = HERE / 'walkthrough.yaml'

#: Six snapshots of demand against four generators. Small enough to print.
GENERATORS = ['wind', 'solar', 'gas', 'oil']
SOURCES = {
    'p_max': pl.DataFrame({'generator': GENERATORS, 'value': [100.0, 60.0, 200.0, 0.0]}),
    'load': pl.DataFrame({'snapshot': range(6), 'value': [80.0, 120.0, 150.0, 180.0, 140.0, 100.0]}),
    'cost': pl.DataFrame({'generator': GENERATORS, 'value': [0.0, 0.0, 50.0, 80.0]}),
}
COORDS = {'snapshot': range(6)}

#: Two ways out of the language, caught at two different stages (see stage 7).
_REFUSED = [
    (
        'a helper that is not in the closed built-in set',
        {
            'constraints': {
                'cumulative': {
                    'foreach': ['snapshot'],
                    'equations': [{'expression': 'cumsum(total_supply) <= load'}],
                }
            }
        },
    ),
    (
        'variable x variable — above the degree-1 ceiling',
        {
            'objectives': {
                'total_cost': {'sense': 'minimize', 'equations': [{'expression': 'sum(p * p, over=generator)'}]}
            }
        },
    ),
]


def banner(n: int, title: str, module: str) -> None:
    print(f'\n{_bold(f"[{n}] {title}")}  {_dim(f"({module})")}')


def main() -> None:
    print(__doc__.split('\n\n')[0])
    print(f'\nmodel: {MODEL.relative_to(HERE.parent)}')

    # ------------------------------------------------------------------
    banner(1, 'YAML text -> validated MathSchema', 'schema.py, validation.py')
    # Parses the file, type-checks it against the pydantic schema, and
    # name-checks every expression, where string, named expression and macro
    # template — used or not. After this call the model is known to be
    # well-formed; no data has been touched.
    schema = fk.load_schema(MODEL)
    print(f'    dimensions   {", ".join(schema.dimensions)}')
    print(f'    parameters   {", ".join(schema.parameters)}')
    print(f'    variables    {", ".join(schema.variables)}')
    print(f'    constraints  {", ".join(schema.constraints)}')
    print(f'    expressions  {", ".join(schema.expressions)}    <- tier 2, still present')
    print(f'    macros       {", ".join(schema.macros)}   <- tier 2, still present')

    # ------------------------------------------------------------------
    banner(2, 'expand macros / named expressions -> core AST', 'expansion.py')
    # Hard rule 1: the core AST is the whole language. Everything above it is
    # pure substitution, which is why a macro costs nothing and cannot make
    # the two lanes disagree — neither lane ever sees one.
    objective_text = schema.objectives['total_cost'].equations[0].expression
    print(f'    written      {objective_text!r}')
    print(f'    parsed       {parse_expression(objective_text)}')
    print(f'    expanded     {parse_and_expand(objective_text, schema)}')
    print('                 ^ the macro is gone: sum(p * cost, over=generator)')

    # ------------------------------------------------------------------
    banner(3, 'core AST -> relational IR', 'lowering.py')
    # This is where the language's boundary is *decided* — by attempting the
    # lowering, so eligibility can never drift from what the backend supports.
    # It needs no data, which is what makes `fk.check()` a CI verb for model
    # repositories: compile the math, bind nothing.
    program = lower_program(schema)
    print('    Program(')
    for decl in (*program.variables, *program.constraints):
        print(f'      {decl}')
    print(f'      {program.objective}')
    print('    )')
    print('    ^ frozen dataclasses, no macro, no YAML, no linopy, no engine')

    # ------------------------------------------------------------------
    banner(4, 'plan + data -> the model frames', 'relational/executor.py')
    # First stage to see a number. Sources are adapted to tidy frames
    # (dims..., value) and the executor assembles the model from them.
    #
    # The private attributes below are the one place this script reaches past
    # the public API: the frames are engine-private by design (hard rule 1 —
    # the plan is internal and the query is backend-private), and looking at
    # them is the whole point here.
    with PolarsExecutor() as ex:
        ex.build(program, tidy_sources(schema, SOURCES, COORDS))
        model = ex._tables()
        for name, frame in (
            ('cols', model.cols),
            ('obj', model.obj),
            ('rows', model.rows),
            ('A', model.matrix),
        ):
            print(f'    {name:<20} {frame.height:>4} rows')
        print('\n    cols/rows/A/obj = the LP itself, in COO form:')
        print('    a column is a bound and a cost, a row a sense and a rhs,')
        print('    and A is every nonzero coefficient as (row, col, coeff).')

        variables = ex._variables['p'].collect()
        n_full = 6 * 4
        print('\n    where "p_max > 0" is not a mask array — it is row absence:')
        print(f'    p has {variables.height} rows, not {n_full}: retired oil never becomes a column.')
        print(_indent(variables.sort('var_label').head(4)))

        # --------------------------------------------------------------
        banner(5, 'sink: stream the frames to an LP file', 'relational/sinks/lp_file.py')
        # Same frames, second sink. The other one (solver_direct, stage 6)
        # hands COO batches to highspy without ever forming a full CSR here.
        with tempfile.TemporaryDirectory() as tmp:
            lp = Path(tmp) / 'model.lp'
            ex.write_lp(lp)
            text = lp.read_text().splitlines()
            print(_indent('\n'.join(text[:12])))
            print(f'    ... ({len(text)} lines total)')

        # --------------------------------------------------------------
        banner(6, 'sink: batches -> highspy -> solution frames', 'relational/sinks/highs.py')
        # Read back by label join, never densified.
        result = ex.solve()
        print(f'    status     {result.status} ({result.termination_condition})')
        print(f'    objective  {result.objective:,.1f}')
        # primal() hands back a frame: the engine's own shape, and the one a
        # caller can pass on without this package depending on their library.
        # Sorted here because a join's output order is the planner's business,
        # not a fact about the architecture this file is narrating.
        print(_indent(result.primal('p').sort('snapshot', 'generator').head(6)))

    # ------------------------------------------------------------------
    banner(7, 'and what the language refuses', 'validation.py, lowering.py')
    # Every rejection is a product statement: the error names the construct
    # and its rewrite. Never a silent fallback, never a redirect to the other
    # lane — both lanes accept exactly the same language (hard rule 3).
    #
    # Each model below is run through `fk.check()` — stages 1-3, no data
    # bound — and then, only if that passes, through a build. Both are caught
    # by `check()`, which is what makes it a CI verb: a model repository can
    # compile-check its math with no data in the runner. The build arm stays
    # because which stage catches what is a real property of the design, and
    # printing it is how this script would tell you if that changed.
    for label, patch in _REFUSED:
        print(f'\n    {label}:')
        model = {**_raw(MODEL), **patch}
        try:
            fk.check(model)
        except ValueError as exc:  # LanguageError is a ValueError subclass
            _refusal('check()', exc)
            continue
        try:
            fk.build(model, SOURCES, coords=COORDS).close()
        except ValueError as exc:
            _refusal('build()', exc)

    print(f'\n{_dim("ARCHITECTURE.md has the rules these stages enforce.")}')


def _refusal(verb: str, exc: Exception) -> None:
    print(f'    {_dim(f"caught by {verb} as {type(exc).__name__}")}')
    print(_indent(str(exc), '      '))


def _bold(text: str) -> str:
    return f'\033[1m{text}\033[0m' if sys.stdout.isatty() else text


def _dim(text: str) -> str:
    return f'\033[2m{text}\033[0m' if sys.stdout.isatty() else text


def _indent(obj: object, pad: str = '    ') -> str:
    return '\n'.join(pad + line for line in str(obj).splitlines())


def _raw(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text())


if __name__ == '__main__':
    main()
