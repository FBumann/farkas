"""The whole architecture, one model, one stage at a time — run it and read.

ARCHITECTURE.md describes the pipeline; this script *executes* it one stage at
a time and prints the artifact each stage produces. Nothing here is a
reimplementation: every call is the same public entry point ``ly.solve`` takes
internally, so what you see is what actually runs.

    uv run python examples/walkthrough.py

Its output is committed as ``examples/walkthrough.out`` and asserted line for
line by ``tests/test_walkthrough.py``, so the narration cannot go stale
unnoticed: a stage that starts saying something else fails CI, and the diff of
the regenerated file is the record of what changed. Everything printed is
therefore deterministic — see ``_scrubbed`` for the one thing that is not.

The point it is trying to make is the thesis in ARCHITECTURE.md: a YAML math
spec is a closed AST known before any data is touched. Stages 1-3 happen with
no data bound at all; only stage 4 sees a number.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pandas as pd

import farkas as ly
from farkas.expansion import parse_and_expand
from farkas.expression_parser import parse_expression
from farkas.lowering import lower_program
from farkas.relational.executor import DuckdbExecutor
from farkas.sources import tidy_sources

HERE = Path(__file__).parent
MODEL = HERE / 'walkthrough.yaml'

#: Six snapshots of demand against four generators. Small enough to print.
SOURCES = {
    'p_max': pd.Series({'wind': 100.0, 'solar': 60.0, 'gas': 200.0, 'oil': 0.0}),
    'load': pd.Series([80.0, 120.0, 150.0, 180.0, 140.0, 100.0], index=pd.RangeIndex(6, name='snapshot')),
    'cost': pd.Series({'wind': 0.0, 'solar': 0.0, 'gas': 50.0, 'oil': 80.0}),
}
COORDS = {'snapshot': pd.RangeIndex(6, name='snapshot')}

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
    schema = ly.load_schema(MODEL)
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
    # It needs no data, which is what makes `ly.check()` a CI verb for model
    # repositories: compile the math, bind nothing.
    program = lower_program(schema)
    print('    Program(')
    for decl in (*program.variables, *program.constraints):
        print(f'      {decl}')
    print(f'      {program.objective}')
    print('    )')
    print('    ^ frozen dataclasses, no macro, no YAML, no linopy, no duckdb')

    # ------------------------------------------------------------------
    banner(4, 'IR + data -> tidy tables in duckdb', 'relational/executor.py')
    # First stage to see a number. Sources are adapted to tidy tables
    # (dims..., value); the executor holds them in a file-backed duckdb under
    # a hard memory_limit — hard rule 4: the full model never resides here.
    #
    # `ex._con` below is the one place this script reaches past the public API:
    # the tables are engine-private by design (hard rule 1 — the IR is internal
    # and SQL is backend-private), and looking at them is the whole point here.
    with DuckdbExecutor(memory_limit='256MB') as ex:
        ex.build(program, tidy_sources(schema, SOURCES, COORDS))
        tables = ex._con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'model' ORDER BY table_name"
        ).fetchall()
        for (name,) in tables:
            n = ex._con.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
            print(f'    {name:<20} {n:>4} rows')
        print(f'\n    memory_limit={ex.memory_limit}, on disk at {_scrubbed(ex.workdir)}')
        print('    dim_* = coordinates · p_* = parameters · var_* = coord -> column label')
        print('    cols/rows/A/obj = the LP itself, in COO form')

        n_var, n_full = ex._con.execute('SELECT count(*) FROM var_p').fetchone()[0], 6 * 4
        print('\n    where "p_max > 0" is not a mask array — it is row absence:')
        print(f'    var_p has {n_var} rows, not {n_full}: retired oil never becomes a column.')
        print(_indent(ex._con.execute('SELECT * FROM var_p ORDER BY var_label LIMIT 4').df()))

        # --------------------------------------------------------------
        banner(5, 'sink: stream the tables to an LP file', 'relational/executor.py')
        # Same tables, second sink. The other one (solver_direct, stage 6)
        # hands COO batches to highspy without ever forming a full CSR here.
        with tempfile.TemporaryDirectory() as tmp:
            lp = Path(tmp) / 'model.lp'
            ex.write_lp(lp)
            text = lp.read_text().splitlines()
            print(_indent('\n'.join(text[:12])))
            print(f'    ... ({len(text)} lines total)')

        # --------------------------------------------------------------
        banner(6, 'sink: batches -> highspy -> solution tables', 'relational/executor.py')
        # Read back by label join, never densified.
        sol = ex.solve()
        print(f'    status     {sol.status}')
        print(f'    objective  {sol.objective:,.1f}')
        # sort explicitly: primal() is a label join, and a join has no
        # inherent row order — leaving it unsorted pins storage layout
        print(_indent(sol.primal('p').sort_values(['snapshot', 'generator'], ignore_index=True).head(6)))

    # ------------------------------------------------------------------
    banner(7, 'and what the language refuses', 'validation.py, lowering.py')
    # Every rejection is a product statement: the error names the construct
    # and its rewrite. Never a silent fallback, never a redirect to the other
    # lane — both lanes accept exactly the same language (hard rule 3).
    #
    # Each model below is run through `ly.check()` — stages 1-3, no data
    # bound — and then, only if that passes, through a build. Both are caught
    # by `check()`, which is what makes it a CI verb: a model repository can
    # compile-check its math with no data in the runner. The build arm stays
    # because which stage catches what is a real property of the design, and
    # printing it is how this script would tell you if that changed.
    for label, patch in _REFUSED:
        print(f'\n    {label}:')
        model = {**_raw(MODEL), **patch}
        try:
            ly.check(model)
        except ValueError as exc:  # LanguageError is a ValueError subclass
            _refusal('check()', exc)
            continue
        try:
            ly.build(model, SOURCES, coords=COORDS).close()
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


def _scrubbed(workdir: Path) -> str:
    """The workdir, minus this run's randomness.

    ``mkdtemp`` appends eight random characters and lives under a temp root
    that differs per machine and per CI runner. Both are real, and neither is
    a fact about the architecture, so the golden file gets the shape instead.
    """
    return f'$TMPDIR/{workdir.name[:-8]}XXXXXXXX'


def _indent(obj: object, pad: str = '    ') -> str:
    return '\n'.join(pad + line for line in str(obj).splitlines())


def _raw(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text())


if __name__ == '__main__':
    main()
