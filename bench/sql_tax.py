"""What the same operator costs in SQL — the complexity tax, measured.

`bench/duckdb-spike.md` §2 claims the porting surface is shallow: every polars
operator the engine uses has a direct SQL counterpart. That is a claim about
*feasibility*, and it is the wrong thing to decide an engine on. This module
answers the other question — what the SQL **reads like** — by porting the
hardest operators for real and diffing the result against the polars compiler
on live data.

    uv run --with duckdb python -m bench.sql_tax

Each case compiles one plan node twice: once through `PolarsCompiler`, once
through SQL written here, over the same dimension and variable tables. Parity
is exact-row equality, so a port that is merely plausible fails. The tax is
then reported as source lines either side, plus what the SQL needed to say it —
CTEs and joins are what make a query hard to read, and they are counted rather
than eyeballed.

Deliberately **not** a full port: five operators, chosen because they are where
§3 says a line-by-line translation is the wrong instinct. If the tax is
acceptable here it is acceptable everywhere; if it is not, the remaining ~2,000
lines will not rescue it.
"""

from __future__ import annotations

import inspect
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lpspec.relational import plan
from lpspec.relational.binding import BoundSources
from lpspec.relational.compiler import PolarsCompiler

if TYPE_CHECKING:
    from collections.abc import Sequence

# --------------------------------------------------------------------------
# A model small enough to diff by eye, shaped to exercise every edge:
# `generator` carries a coordinate (for GroupSum), `p` is masked (so absence
# has something to propagate), and `snapshot` is long enough that a shift by 2
# leaves a real edge rather than an empty one.
# --------------------------------------------------------------------------

SNAPSHOTS = [0, 1, 2, 3, 4, 5]
GENERATORS = ['wind', 'solar', 'gas']
BUSES = ['north', 'south']
#: `gas` is masked off at snapshots 0 and 1 — absence with a hole in it, not a
#: whole label missing, which is the case a semi-join on the label alone passes.
MASKED_OFF = {(0, 'gas'), (1, 'gas')}

PROGRAM = plan.Program(
    parameters=(
        plan.ParameterDeclaration('cost', ('generator',)),
        plan.ParameterDeclaration('load', ('snapshot',)),
    ),
    variables=(plan.VariableDeclaration('p', ('snapshot', 'generator'), where=plan.BooleanConstant(True)),),
    constraints=(),
    objective=plan.ObjectiveDeclaration('min', plan.Variable('p')),
    dimensions=(
        plan.DimensionDeclaration('snapshot'),
        plan.DimensionDeclaration('generator', coordinates=(('bus', 'bus'),)),
        plan.DimensionDeclaration('bus'),
    ),
)
CARDINALITY = {'snapshot': len(SNAPSHOTS), 'generator': len(GENERATORS), 'bus': len(BUSES)}


def _tables() -> tuple[dict[str, pl.DataFrame], dict[str, pl.DataFrame], dict[str, pl.DataFrame]]:
    """Dimension, parameter and variable tables, as the executor would lay them out."""
    dims = {
        'snapshot': pl.DataFrame({'val': SNAPSHOTS, 'ord': list(range(len(SNAPSHOTS)))}),
        'generator': pl.DataFrame(
            {
                'val': GENERATORS,
                'ord': list(range(len(GENERATORS))),
                'bus': ['north', 'north', 'south'],
            }
        ),
        'bus': pl.DataFrame({'val': BUSES, 'ord': list(range(len(BUSES)))}),
    }
    params = {
        'cost': pl.DataFrame({'generator': GENERATORS, 'value': [1.0, 2.0, 50.0]}),
        'load': pl.DataFrame({'snapshot': SNAPSHOTS, 'value': [12.0, 8.0, 20.0, 15.0, 9.0, 11.0]}),
    }
    live = [(s, g) for s in SNAPSHOTS for g in GENERATORS if (s, g) not in MASKED_OFF]
    variables = {
        'p': pl.DataFrame(
            {
                'snapshot': [s for s, _ in live],
                'generator': [g for _, g in live],
                'var_label': list(range(len(live))),
            }
        )
    }
    return dims, params, variables


def _compiler() -> PolarsCompiler:
    dims, params, variables = _tables()
    bound = BoundSources(
        parameters={k: v.lazy() for k, v in params.items()},
        dimensions={k: v.lazy() for k, v in dims.items()},
        cardinality=CARDINALITY,
        boolean_parameters=frozenset(),
    )
    return PolarsCompiler(PROGRAM, bound, {k: v.lazy() for k, v in variables.items()})


def _connect():  # noqa: ANN202
    import duckdb

    con = duckdb.connect()
    dims, params, variables = _tables()
    for name, frame in dims.items():
        con.register(f'dim_{name}', frame.to_arrow())
    for name, frame in params.items():
        con.register(f'par_{name}', frame.to_arrow())
    for name, frame in variables.items():
        con.register(f'var_{name}', frame.to_arrow())
    return con


# --------------------------------------------------------------------------
# The cases. `polars` returns the frame the real compiler produces; `sql` is
# the port. Both must yield the same rows, compared as sets.
# --------------------------------------------------------------------------


def case_sum(q: PolarsCompiler) -> pl.LazyFrame:
    """Sum over a dim. **Projection, not aggregation** — the rows survive."""
    frag = q._sum_fragment(q._variable_fragment('p'), ('generator',), 'sum')
    return frag.frame


SQL_SUM = """
-- Sum drops the dim and keeps the rows; the collapse happens once, in the
-- terminal aggregate at assembly. `SELECT` without `GROUP BY` is the whole port.
SELECT snapshot, var_label, 1.0 AS coeff FROM var_p
"""


def case_group_sum(q: PolarsCompiler) -> pl.LazyFrame:
    """Relabel a dim through a declared coordinate."""
    frag = q._group_fragment(
        q._variable_fragment('p'),
        plan.GroupSum(plan.Variable('p'), over='generator', coordinate='bus', into='bus'),
        'group',
    )
    return frag.frame


SQL_GROUP_SUM = """
-- One inner join against the dim table, which holds one row per label, so the
-- join neither duplicates nor drops a term.
SELECT v.snapshot, d.bus AS bus, v.var_label, 1.0 AS coeff
FROM var_p v JOIN dim_generator d ON v.generator = d.val
"""


def case_translate_wrap(q: PolarsCompiler) -> pl.LazyFrame:
    """Cyclic roll: a row at ordinal *o* contributes at ``(o + by) % card``."""
    frag = q._translate_fragment(
        q._variable_fragment('p'),
        plan.Translate(plan.Variable('p'), dimension='snapshot', by=2, wrap=True),
        'roll',
    )
    return frag.frame


SQL_TRANSLATE_WRAP = """
-- Two joins on the dim table, not a window: the ordinal is looked up, moved,
-- and looked back. That is what keeps this bounded-halo rather than global.
-- The doubled modulo is not redundant — SQL's % keeps the sign of the operand,
-- so a negative `by` would otherwise land outside the table and simply fail to
-- join, silently dropping the row instead of wrapping it.
SELECT v.generator, o.val AS snapshot, v.var_label, 1.0 AS coeff
FROM var_p v
JOIN dim_snapshot i ON v.snapshot = i.val
JOIN dim_snapshot o ON o.ord = ((i.ord + 2) % 6 + 6) % 6
"""


def case_translate_fill(q: PolarsCompiler) -> pl.LazyFrame:
    """Acyclic shift with ``fill=0``: the vacated edge becomes *present*.

    The tax case. `fill=0` does not add a term — a missing row already reads as
    zero — it puts the edge coordinates back into the **presence** set, so the
    row survives with no contribution instead of being dropped by absence
    propagation. So the interesting output here is `presence`, not `frame`.
    """
    frag = q._translate_fragment(
        q._variable_fragment('p'),
        plan.Translate(plan.Variable('p'), dimension='snapshot', by=2, wrap=False, fill=0.0),
        'shift',
    )
    return frag.presence


SQL_TRANSLATE_FILL = """
-- The presence set after an acyclic shift: what moved, plus what the edge
-- vacated. Both halves are needed and they are complements, so the edge is one
-- predicate negated rather than two conditions kept in step.
WITH moved AS (
    SELECT v.generator, o.val AS snapshot
    FROM var_p v
    JOIN dim_snapshot i ON v.snapshot = i.val
    JOIN dim_snapshot o ON o.ord = i.ord + 2      -- no wrap: out of range does not join
),
edge AS (                                          -- ordinals with nothing to move in
    SELECT val AS snapshot FROM dim_snapshot WHERE ord - 2 < 0 OR ord - 2 >= 6
),
vacated AS (                                       -- one edge row per other-dim combo
    SELECT o.generator, e.snapshot
    FROM (SELECT DISTINCT generator FROM var_p) o CROSS JOIN edge e
)
SELECT generator, snapshot FROM moved
UNION                                              -- UNION, not UNION ALL: .unique()
SELECT generator, snapshot FROM vacated
"""


def case_masked_label(q: PolarsCompiler) -> pl.LazyFrame:
    """Dense ``0..n-1`` over a masked coordinate product — the counted path.

    The one operator whose *order* is load-bearing: `var_label` is the solver's
    own column index, so two builds must agree on it integer for integer.
    """
    from lpspec.relational.labels import Labeller

    labeller = Labeller(q, CARDINALITY, PROGRAM)
    frame, _ = labeller.frame(
        ('snapshot', 'generator'),
        plan.BooleanConstant(True),
        'var_label',
        0,
        restrictions=[(('snapshot', 'generator'), q.variables['p'].select('snapshot', 'generator'))],
    )
    return frame.lazy()


SQL_MASKED_LABEL = """
-- ROW_NUMBER over the ordered masked product. Ordered by the *ordinals*, not
-- the labels, because declaration order is what makes a label the solver's
-- index -- ordering by 'gas' < 'solar' < 'wind' would be a different model.
SELECT p.snapshot, p.generator,
       ROW_NUMBER() OVER (ORDER BY s.ord, g.ord) - 1 AS var_label
FROM (SELECT DISTINCT snapshot, generator FROM var_p) p
JOIN dim_snapshot s ON p.snapshot = s.val
JOIN dim_generator g ON p.generator = g.val
"""


CASES: list[tuple[str, Callable[[PolarsCompiler], pl.LazyFrame], str, Sequence[str]]] = [
    ('Sum', case_sum, SQL_SUM, ('_sum_fragment',)),
    ('GroupSum', case_group_sum, SQL_GROUP_SUM, ('_group_fragment',)),
    ('Translate (wrap)', case_translate_wrap, SQL_TRANSLATE_WRAP, ('_translate_fragment',)),
    (
        'Translate (fill, presence)',
        case_translate_fill,
        SQL_TRANSLATE_FILL,
        ('_translate_fragment', '_edge', '_vacated'),
    ),
    ('Masked label', case_masked_label, SQL_MASKED_LABEL, ('frame',)),
]


def _body_lines(fn: Any) -> int:
    """Source lines of *fn*, excluding its docstring and blank/comment lines.

    Comments are excluded on both sides: the polars methods are heavily
    commented and the SQL is too, and counting either would measure the prose
    rather than the operator.
    """
    src = textwrap.dedent(inspect.getsource(fn))
    tree = src.splitlines()
    out, in_doc = 0, False
    for raw in tree[1:]:  # skip the `def` line
        line = raw.strip()
        if line.startswith(('"""', "'''")):
            # a one-line docstring opens and closes on the same line
            in_doc = not (len(line) > 5 and line.endswith(('"""', "'''")))
            continue
        if in_doc or not line or line.startswith('#'):
            continue
        out += 1
    return out


def _sql_lines(sql: str) -> int:
    return sum(1 for ln in sql.strip().splitlines() if ln.strip() and not ln.strip().startswith('--'))


def _norm(frame: pl.DataFrame) -> list[tuple[Any, ...]]:
    return sorted(frame.sort(frame.columns).rows())


def main() -> int:
    q = _compiler()
    con = _connect()
    from lpspec.relational import compiler as compiler_module
    from lpspec.relational import labels as labels_module

    rows = []
    ok = True
    for name, fn, sql, methods in CASES:
        want = fn(q).collect()
        got = pl.from_arrow(con.execute(sql).fetch_arrow_table())
        assert isinstance(got, pl.DataFrame)
        # column order is a projection detail, not a semantic one; compare the
        # same columns in the same order on both sides
        got = got.select(want.columns)
        same = _norm(want) == _norm(got)
        ok &= same
        owner = labels_module.Labeller if name == 'Masked label' else compiler_module.PolarsCompiler
        polars_lines = sum(_body_lines(getattr(owner, m)) for m in methods)
        rows.append((name, 'same' if same else 'DIFFERENT', want.height, polars_lines, _sql_lines(sql),
                     sql.count('JOIN'), sql.upper().count(' AS (')))

    width = max(len(r[0]) for r in rows)
    print(f'{"operator".ljust(width)}  parity     rows  polars  sql  joins  CTEs')
    for name, verdict, height, pl_lines, sql_lines, joins, ctes in rows:
        print(f'{name.ljust(width)}  {verdict:<9} {height:>4}  {pl_lines:>6}  {sql_lines:>3}  {joins:>5}  {ctes:>4}')
    print()
    print('polars = source lines of the compiler method(s), docstrings/comments excluded.')
    print('sql    = non-comment lines of the port. joins/CTEs are what a reader pays.')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
