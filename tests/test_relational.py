"""Phase-2 gate: two real models round-trip through solve on the relational backend.

Each model is built three ways and must agree on the objective:
  1. relational executor -> solver_direct (HiGHS via batched addCols/addRows)
  2. relational executor -> lp_file sink -> HiGHS reads and solves the file
  3. eager linopy build (the correctness oracle)
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

import farkas as fk
from farkas.errors import DataError, LanguageError
from farkas.relational import (
    DuckdbExecutor,
    chunking,
)
from farkas.relational.plan import (
    Constant,
    ConstraintDeclaration,
    DimensionComparison,
    DimensionDeclaration,
    GroupSum,
    ObjectiveDeclaration,
    Parameter,
    ParameterComparison,
    ParameterDeclaration,
    Program,
    Sum,
    Variable,
    VariableDeclaration,
)
from tests.conftest import solve_lp_file
from tests.differential import RTOL
from tests.oracle import linopy, transport_eager_objective, xr

# ---------------------------------------------------------------------------
# model 1: dispatch (the spec example)
# ---------------------------------------------------------------------------


@pytest.fixture
def dispatch_data():
    rng = np.random.default_rng(7)
    n_s, n_g = 40, 6
    gens = pd.DataFrame(
        {
            'generator': [f'g{i}' for i in range(n_g)],
            'p_max': rng.uniform(50, 200, n_g).round(3),
            'cost': rng.uniform(5, 100, n_g).round(3),
        }
    )
    gens.loc[1, 'p_max'] = 0.0  # masked out by where
    load = pd.DataFrame(
        {
            'snapshot': np.arange(n_s),
            'value': (rng.uniform(0.2, 0.7, n_s) * gens['p_max'].sum()).round(3),
        }
    )
    return gens, load


def dispatch_program() -> Program:
    return Program(
        parameters=(
            ParameterDeclaration('p_max', ('generator',)),
            ParameterDeclaration('cost', ('generator',)),
            ParameterDeclaration('load', ('snapshot',)),
        ),
        variables=(
            VariableDeclaration(
                'p',
                ('snapshot', 'generator'),
                where=ParameterComparison('p_max', '>', 0),
                lower=Constant(0.0),
                upper=Parameter('p_max'),
            ),
        ),
        constraints=(
            ConstraintDeclaration(
                'power_balance',
                ('snapshot',),
                lhs=Sum(Variable('p'), over=('generator',)),
                sense='==',
                rhs=Parameter('load'),
            ),
        ),
        objective=ObjectiveDeclaration('min', Sum(Variable('p') * Parameter('cost'), over=('generator', 'snapshot'))),
    )


def dispatch_sources(gens: pd.DataFrame, load: pd.DataFrame) -> dict:
    return {
        'p_max': gens[['generator', 'p_max']].rename(columns={'p_max': 'value'}),
        'cost': gens[['generator', 'cost']].rename(columns={'cost': 'value'}),
        'load': load,
        'snapshot': load[['snapshot']],
    }


def dispatch_eager_objective(gens: pd.DataFrame, load: pd.DataFrame) -> float:
    gi = gens.set_index('generator')
    li = load.set_index('snapshot')['value']
    p_max = xr.DataArray.from_series(gi['p_max'])
    cost = xr.DataArray.from_series(gi['cost'])
    load_da = xr.DataArray.from_series(li)

    m = linopy.Model()
    mask = (p_max > 0).broadcast_like(load_da * p_max)
    p = m.add_variables(lower=0, upper=p_max, coords=[li.index, gi.index], name='p', mask=mask)
    m.add_constraints(p.sum('generator') == load_da, name='power_balance')
    m.add_objective((p * cost).sum())
    m.solve(solver_name='highs', output_flag=False)
    return float(m.objective.value)


def test_dispatch_roundtrip(dispatch_data, tmp_path):
    gens, load = dispatch_data
    oracle = dispatch_eager_objective(gens, load)

    with DuckdbExecutor(memory_limit='256MB', chunk_rows=500) as ex:
        ex.build(dispatch_program(), dispatch_sources(gens, load))

        result = ex.solve()
        assert result.is_ok
        assert result.objective == pytest.approx(oracle, rel=RTOL)

        lp = tmp_path / 'dispatch.lp'
        ex.write_lp(lp)
        assert solve_lp_file(lp) == pytest.approx(oracle, rel=RTOL)

        # masked variable rows are absent, and primal joins back to coords
        primal = result.primal('p')
        n_active = int((gens['p_max'] > 0).sum())
        assert len(primal) == n_active * len(load)
        assert set(primal.columns) == {'snapshot', 'generator', 'value'}
        # per-snapshot dispatch matches load
        balance = primal.groupby('snapshot')['value'].sum()
        expected = load.set_index('snapshot')['value']
        assert np.allclose(balance.sort_index(), expected.sort_index())


# ---------------------------------------------------------------------------
# model 2: multi-bus transport (exercises GroupSum and signed flows)
# ---------------------------------------------------------------------------


def transport_program() -> Program:
    injection = (
        GroupSum(Variable('p'), over='generator', coordinate='bus', into='bus')
        + GroupSum(Variable('f'), over='line', coordinate='to', into='bus')
        - GroupSum(Variable('f'), over='line', coordinate='from', into='bus')
    )
    return Program(
        parameters=(
            ParameterDeclaration('p_max', ('generator',)),
            ParameterDeclaration('cost', ('generator',)),
            ParameterDeclaration('cap', ('line',)),
            ParameterDeclaration('load', ('snapshot', 'bus')),
        ),
        variables=(
            VariableDeclaration(
                'p',
                ('snapshot', 'generator'),
                lower=Constant(0.0),
                upper=Parameter('p_max'),
            ),
            VariableDeclaration(
                'f',
                ('snapshot', 'line'),
                lower=-Parameter('cap'),
                upper=Parameter('cap'),
            ),
        ),
        constraints=(
            ConstraintDeclaration(
                'balance',
                ('snapshot', 'bus'),
                lhs=injection,
                sense='==',
                rhs=Parameter('load'),
            ),
        ),
        objective=ObjectiveDeclaration('min', Sum(Variable('p') * Parameter('cost'), over=('generator', 'snapshot'))),
        dimensions=(
            DimensionDeclaration('generator', (('bus', 'bus'),)),
            DimensionDeclaration('line', (('from', 'bus'), ('to', 'bus'))),
        ),
    )


def transport_sources(gens, lines, load) -> dict:
    return {
        'p_max': gens[['generator', 'p_max']].rename(columns={'p_max': 'value'}),
        'cost': gens[['generator', 'cost']].rename(columns={'cost': 'value'}),
        'cap': lines[['line', 'cap']].rename(columns={'cap': 'value'}),
        'load': load,
        'snapshot': load[['snapshot']],
        'bus': load[['bus']],
        # dims carrying declared coordinates need an index source that has them
        'generator': gens[['generator', 'bus']],
        'line': lines[['line', 'from_bus', 'to_bus']].rename(columns={'from_bus': 'from', 'to_bus': 'to'}),
    }


def test_transport_roundtrip(transport_data, tmp_path):
    gens, lines, load = transport_data
    oracle = transport_eager_objective(gens, lines, load)
    assert np.isfinite(oracle), 'oracle model must be feasible'

    with DuckdbExecutor(memory_limit='256MB', chunk_rows=300) as ex:
        ex.build(transport_program(), transport_sources(gens, lines, load))

        result = ex.solve()
        assert result.is_ok
        assert result.objective == pytest.approx(oracle, rel=RTOL)

        lp = tmp_path / 'transport.lp'
        ex.write_lp(lp)
        assert solve_lp_file(lp) == pytest.approx(oracle, rel=RTOL)

        # flows respect line capacity bounds
        primal_f = result.primal('f')
        caps = lines.set_index('line')['cap']
        limits = primal_f['line'].map(caps)
        assert (primal_f['value'].abs() <= limits + 1e-6).all()


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------


def test_a_mask_that_removes_nothing_labels_exactly_like_no_mask(dispatch_data):
    """The two paths through ``_label_frame`` must agree row for row.

    Unmasked, a label is arithmetic on the dim ordinals; masked, it is counted
    by a chunked ``ROW_NUMBER``. A label *is* the solver column index, so
    "same number of rows" is not the property that matters — which coordinate
    got which index is. ``chunk_rows`` is small enough to force several chunks,
    since a disagreement would live in the running offset between them.
    """
    gens, load = dispatch_data
    gens = gens.assign(p_max=gens['p_max'].where(gens['p_max'] > 0, 1.0))  # leave the mask nothing to remove
    variable = dispatch_program().variables[0]

    labelled = {}
    for name, where in (('masked', ParameterComparison('p_max', '>', 0)), ('unmasked', None)):
        program = replace(dispatch_program(), variables=(replace(variable, where=where),))
        with DuckdbExecutor(memory_limit='256MB', chunk_rows=60) as ex:
            ex.build(program, dispatch_sources(gens, load))
            labelled[name] = ex._con.execute('SELECT snapshot, generator, var_label FROM var_p ORDER BY var_label').df()

    assert len(labelled['unmasked']) == len(gens) * len(load)
    pd.testing.assert_frame_equal(labelled['masked'], labelled['unmasked'])


def _label_program(where):
    """One variable over three dims of distinct cardinality, masked or not."""
    return Program(
        parameters=(ParameterDeclaration('cap', ('line',)),),
        variables=(
            VariableDeclaration(
                'f', ('snapshot', 'bus', 'line'), where=where, lower=Constant(0.0), upper=Constant(1.0)
            ),
        ),
        constraints=(
            ConstraintDeclaration(
                'cap_row',
                ('snapshot',),
                lhs=Sum(Variable('f'), over=('bus', 'line')),
                sense='<=',
                rhs=Constant(1e6),
            ),
        ),
        objective=ObjectiveDeclaration('min', Sum(Variable('f'), over=('snapshot', 'bus', 'line'))),
    )


def _label_sources(n_snapshot=5, n_bus=3, n_line=4, zero_caps=()):
    lines = [f'l{i}' for i in range(n_line)]
    caps = [0.0 if i in zero_caps else 10.0 + i for i in range(n_line)]
    return {
        'cap': pd.DataFrame({'line': lines, 'value': caps}),
        'snapshot': pd.DataFrame({'snapshot': np.arange(n_snapshot)}),
        'bus': pd.DataFrame({'bus': [f'b{i}' for i in range(n_bus)]}),
        'line': pd.DataFrame({'line': lines}),
    }


@pytest.mark.parametrize('chunk_rows', [12, 1_000_000], ids=['chunked', 'single-chunk'])
def test_unmasked_labels_are_the_ones_the_window_would_assign(chunk_rows):
    """The arithmetic path must emit the labels the sort it replaces did.

    Unmasked frames skip ``ROW_NUMBER`` for a closed form
    (``SqlCompiler.positional_label``). Labels *are* the solver's column
    indices, so "dense and valid" is not the bar — they have to be the same
    integers, or the LP section order, ``solver_direct``'s batching and the
    walkthrough golden all shift underneath us for no stated reason.

    Three dims of distinct cardinality, so a swapped stride cannot pass. The
    expectation is spelled out rather than computed, so it cannot agree with
    the implementation by sharing its arithmetic.
    """
    with DuckdbExecutor(memory_limit='256MB', chunk_rows=chunk_rows) as ex:
        ex.build(_label_program(where=None), _label_sources())
        got = ex._con.execute('SELECT snapshot, bus, line, var_label FROM var_f ORDER BY var_label').fetchall()

    # what ROW_NUMBER() OVER (ORDER BY ord, ord, ord) means, spelled out
    expected = [
        (s, f'b{b}', f'l{ln}', i)
        for i, (s, b, ln) in enumerate((s, b, ln) for s in range(5) for b in range(3) for ln in range(4))
    ]
    assert got == expected


@pytest.mark.parametrize('chunk_rows', [12, 1_000_000], ids=['chunked', 'single-chunk'])
def test_masked_labels_stay_dense(chunk_rows):
    """The counted path stays a dense bijection, across chunk boundaries.

    A mask that reads the *leading* dim cannot factor, so this is the general
    ``ROW_NUMBER`` path — the one whose density comes from the running offset
    stitching the per-chunk windows into one range, rather than from
    arithmetic. Both chunk regimes, since that offset is what a single chunk
    never exercises.
    """
    where = DimensionComparison('snapshot', '>', 0)
    with DuckdbExecutor(memory_limit='256MB', chunk_rows=chunk_rows) as ex:
        ex.build(_label_program(where), _label_sources())
        labels = [r[0] for r in ex._con.execute('SELECT var_label FROM var_f ORDER BY var_label').fetchall()]

    assert labels == list(range(4 * 3 * 4))  # one snapshot of five masked out everywhere


def test_a_mask_on_the_trailing_dims_labels_like_the_window_would():
    """The factoring path and the counted path must agree integer for integer.

    When the mask does not read the leading dim, ``_label_frame`` ranks the
    surviving trailing coordinates once and multiplies out. That is a third
    way to reach a label, so it is pinned against the same spelled-out
    row-major expectation: a mask removing line ``l1`` leaves the remaining
    three lines in order within each ``(snapshot, bus)`` block.
    """
    where = ParameterComparison('cap', '>', 0)
    with DuckdbExecutor(memory_limit='256MB', chunk_rows=12) as ex:
        ex.build(_label_program(where), _label_sources(zero_caps=(1,)))
        got = ex._con.execute('SELECT snapshot, bus, line, var_label FROM var_f ORDER BY var_label').fetchall()

    expected = [
        (s, f'b{b}', f'l{ln}', i)
        for i, (s, b, ln) in enumerate((s, b, ln) for s in range(5) for b in range(3) for ln in (0, 2, 3))
    ]
    assert got == expected


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


def test_nonlinear_product_rejected(dispatch_data):
    gens, load = dispatch_data
    prog = dispatch_program()
    bad = Program(
        parameters=prog.parameters,
        variables=prog.variables,
        constraints=prog.constraints,
        objective=ObjectiveDeclaration('min', Sum(Variable('p') * Variable('p'), over=('generator', 'snapshot'))),
    )
    with DuckdbExecutor() as ex, pytest.raises(LanguageError, match='nonlinear'):
        ex.build(bad, dispatch_sources(gens, load))


def test_missing_source_rejected(dispatch_data):
    gens, load = dispatch_data
    sources = dispatch_sources(gens, load)
    del sources['cost']
    with DuckdbExecutor() as ex, pytest.raises(DataError, match="no source bound for parameter 'cost'"):
        ex.build(dispatch_program(), sources)


#: A scalar parameter used in a bound and in the objective — the two places a
#: silent row multiplication is least visible.
SCALAR_MODEL = {
    'dimensions': {'i': {'dtype': 'int', 'values': [0, 1]}},
    'parameters': {'s': {'dims': []}},
    'variables': {'x': {'foreach': ['i'], 'bounds': {'lower': 0, 'upper': 's'}}},
    'constraints': {'floor': {'foreach': ['i'], 'equations': [{'expression': 'x >= 1'}]}},
    'objectives': {'total': {'sense': 'minimize', 'equations': [{'expression': 'sum(x * s, over=i)'}]}},
}


@pytest.mark.parametrize('rows', [2, 0])
def test_a_dimensionless_parameter_must_be_one_row(rows):
    """No dims means one value broadcast everywhere, and nothing used to check it.

    The join that broadcasts a dimensionless parameter is ``ON TRUE``, which is
    right for one row and a silent row multiplication for two — duplicate
    columns for one variable in a bound, duplicate mask rows in a where. The
    per-coordinate check next door skipped this case entirely, because a
    parameter with no dims has nothing to group by (#166).
    """
    data = {'s': pd.DataFrame({'value': pd.Series([1.0] * rows, dtype='float64')})}
    with pytest.raises(DataError, match=f"parameter 's' .* its source has {rows} rows"):
        fk.build(SCALAR_MODEL, data)


def test_a_dimensionless_parameter_of_one_row_still_builds():
    """The control: the shape the check exists to let through."""
    data = {'s': pd.DataFrame({'value': [10.0]})}
    with fk.solve(SCALAR_MODEL, data) as result:
        assert result.objective == pytest.approx(20.0)  # x == 1 at both coordinates of i, times s


def test_out_of_foreach_dims_rejected(dispatch_data):
    gens, load = dispatch_data
    prog = dispatch_program()
    bad = Program(
        parameters=prog.parameters,
        variables=prog.variables,
        constraints=(
            ConstraintDeclaration(
                'power_balance',
                ('snapshot',),
                lhs=Variable('p'),  # generator dim not summed
                sense='==',
                rhs=Parameter('load'),
            ),
        ),
        objective=prog.objective,
    )
    with DuckdbExecutor() as ex, pytest.raises(LanguageError, match='missing a Sum'):
        ex.build(bad, dispatch_sources(gens, load))


def test_a_quote_in_a_path_does_not_end_the_statement(tmp_path):
    """Paths come from the calling program, so no language rule constrains them.

    `o'brien` is a legal directory name; interpolated raw it closed the SQL
    string literal and duckdb raised a ParserException — which is not even a
    LinopyYamlError, so it escaped the package's own exception tree. Every
    path-carrying sink and source is exercised here: a parquet source, an
    explicit index source, the LP writer's workdir, and the parquet sink.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    odd = tmp_path / "o'brien"
    odd.mkdir()
    pq.write_table(pa.table({'snapshot': [0, 1], 'value': [1.0, 2.0]}), odd / 'load.parquet')
    pq.write_table(pa.table({'snapshot': [0, 1]}), odd / 'index.parquet')

    model = {
        'dimensions': {'snapshot': {'dtype': 'int'}},
        'parameters': {'load': {'dims': ['snapshot']}},
        'variables': {'p': {'foreach': ['snapshot'], 'bounds': {'lower': 0}}},
        'constraints': {'meet': {'foreach': ['snapshot'], 'equations': [{'expression': 'p >= load'}]}},
        'objectives': {'c': {'sense': 'minimize', 'equations': [{'expression': 'sum(p, over=snapshot)'}]}},
    }
    sources = {'load': str(odd / 'load.parquet'), 'snapshot': str(odd / 'index.parquet')}

    with fk.solve(model, sources, workdir=str(odd / 'work')) as solution:
        assert solution.is_ok
        assert solution.objective == pytest.approx(3.0)
        written = solution.to_parquet(odd / 'out')
        assert written['p'].exists()

    fk.write(model, sources, odd / 'model.lp')
    assert (odd / 'model.lp').stat().st_size > 0


# ---------------------------------------------------------------------------
# objective assembly
# ---------------------------------------------------------------------------


def _objective_program(expression):
    """One masked variable over two dims, and whatever objective is passed."""
    return Program(
        parameters=(
            ParameterDeclaration('p_max', ('generator',)),
            ParameterDeclaration('cost', ('generator',)),
            ParameterDeclaration('load', ('snapshot',)),
        ),
        variables=(
            VariableDeclaration(
                'p',
                ('snapshot', 'generator'),
                where=ParameterComparison('p_max', '>', 0),
                lower=Constant(0.0),
                upper=Parameter('p_max'),
            ),
        ),
        constraints=(
            ConstraintDeclaration(
                'power_balance',
                ('snapshot',),
                lhs=Sum(Variable('p'), over=('generator',)),
                sense='==',
                rhs=Parameter('load'),
            ),
        ),
        objective=ObjectiveDeclaration('min', expression),
    )


def _objective_coefficients(expression, dispatch_data):
    """``obj`` as ``{col: coeff}``, plus whether the aggregate was skipped."""
    gens, load = dispatch_data
    sources = {
        'p_max': gens[['generator', 'p_max']].rename(columns={'p_max': 'value'}),
        'cost': gens[['generator', 'cost']].rename(columns={'cost': 'value'}),
        'load': load,
    }
    program = _objective_program(expression)
    with DuckdbExecutor(memory_limit='256MB') as ex:
        ex.build(program, sources)
        compiled = ex._sql.expression(expression, 'objective')
        skipped = ex._objective_column_appears_once(expression, compiled)
        rows = ex._con.execute('SELECT col, coeff FROM obj ORDER BY col').fetchall()
    return dict(rows), skipped


def test_objective_skips_the_aggregate_only_when_a_column_cannot_repeat(dispatch_data):
    """``p * cost`` needs no ``GROUP BY``; ``p * cost + p * cost`` does.

    The aggregate merges terms landing on the same column. Skipping it when a
    column genuinely repeats would silently halve those coefficients — an LP
    that builds, solves, and answers a different question. So the two shapes
    are asserted together: the fast path must fire on the first *and* must not
    fire on the second, and the coefficients must come out the same either way.
    """
    single = Variable('p') * Parameter('cost')
    doubled = single + single

    one, skipped_one = _objective_coefficients(single, dispatch_data)
    two, skipped_two = _objective_coefficients(doubled, dispatch_data)

    assert skipped_one, 'p * cost has one row per column — the aggregate is dead weight'
    assert not skipped_two, 'the same variable twice must keep the aggregate'

    # the aggregate is what makes the second objective twice the first
    assert one, 'the objective produced no coefficients at all'
    assert two == pytest.approx({col: 2.0 * coeff for col, coeff in one.items()})


def test_objective_keeps_the_aggregate_when_a_reduction_hides_extra_rows():
    """A fragment's dims can match the variable's while its rows do not.

    ``sum(q * price, over=generator)`` with ``q`` indexed by snapshot alone
    reduces to dims ``('snapshot',)`` — exactly ``q``'s declaration — but
    ``_sum_fragment`` *projects*, it does not aggregate, so the fragment still
    carries one row per generator. Skipping the ``GROUP BY`` there writes
    ``|generator|`` rows into ``obj`` for a single column: the LP file would
    quietly re-sum them, while ``cols LEFT JOIN obj`` in the HiGHS sink would
    hand the solver more columns than the model has.

    This is the case a dims-equality test alone gets wrong, so it is the case
    that is pinned.
    """
    program = Program(
        parameters=(
            ParameterDeclaration('price', ('snapshot', 'generator')),
            ParameterDeclaration('load', ('snapshot',)),
        ),
        variables=(VariableDeclaration('q', ('snapshot',), where=None, lower=Constant(0.0), upper=Constant(10.0)),),
        constraints=(
            ConstraintDeclaration('floor', ('snapshot',), lhs=Variable('q'), sense='>=', rhs=Parameter('load')),
        ),
        objective=ObjectiveDeclaration('min', Sum(Variable('q') * Parameter('price'), over=('generator',))),
    )
    sources = {
        'price': pd.DataFrame(
            {'snapshot': np.repeat([0, 1], 3), 'generator': ['g0', 'g1', 'g2'] * 2, 'value': [1.0, 2.0, 3.0] * 2}
        ),
        'load': pd.DataFrame({'snapshot': [0, 1], 'value': [5.0, 5.0]}),
    }

    with DuckdbExecutor(memory_limit='256MB') as ex:
        ex.build(program, sources)
        rows = ex._con.execute('SELECT col, coeff FROM obj ORDER BY col').fetchall()

    # one row per column, the three generator prices summed — not three rows
    assert rows == [(0, 6.0), (1, 6.0)]


@pytest.mark.parametrize('batch_rows', [2, 3, 100_000], ids=['ragged', 'exact', 'one-chunk'])
def test_infinite_bounds_survive_the_arrow_handoff(batch_rows):
    """An absent upper bound must reach HiGHS as infinity, not as a number.

    The column loop converts Arrow to numpy directly rather than through
    ``to_pydict``, and infinities are the values a conversion is most likely to
    mangle — ``nan_to_num`` maps them by keyword, and a column that arrived as
    nan instead of inf would silently become ``0.0``. A finite upper bound here
    is not a wrong answer, it is a *different model*: minimising still succeeds
    while maximising stops being unbounded. So both directions are asserted,
    and the batch sizes make the infinities straddle a chunk boundary.
    """
    program = Program(
        parameters=(ParameterDeclaration('floor', ('snapshot',)),),
        variables=(
            VariableDeclaration('x', ('snapshot',), where=None, lower=Parameter('floor'), upper=Constant(float('inf'))),
        ),
        constraints=(),
        objective=ObjectiveDeclaration('min', Sum(Variable('x'), over=('snapshot',))),
    )
    sources = {'floor': pd.DataFrame({'snapshot': [0, 1, 2], 'value': [5.0, 7.0, 9.0]})}

    with DuckdbExecutor(memory_limit='256MB') as ex:
        ex.build(program, sources)
        assert ex._con.execute("SELECT count(*) FROM cols WHERE ub = 'infinity'::DOUBLE").fetchone()[0] == 3
        result = ex.solve(batch_rows=batch_rows)
        assert result.is_ok
        assert result.objective == pytest.approx(21.0, rel=RTOL)

    with DuckdbExecutor(memory_limit='256MB') as ex:
        ex.build(
            replace(program, objective=ObjectiveDeclaration('max', Sum(Variable('x'), over=('snapshot',)))), sources
        )
        unbounded = ex.solve(batch_rows=batch_rows)
        assert unbounded.termination_condition in ('unbounded', 'infeasible_or_unbounded'), (
            f'a finite upper bound reached the solver: {unbounded.termination_condition}'
        )


def test_row_chunks_are_bounded_by_nonzeros_not_by_rows():
    """A chunk of rows is a chunk of *entries*, and only entries are residency.

    Both sinks read ``A`` a range at a time and hold that range while they work
    it. Sizing the range in rows bounds the wrong quantity: the same 100k-row
    range is 900k entries in ``transport`` and 10M in ``dispatch``, so what a
    sink actually holds is set by the model's shape rather than by the budget —
    which is precisely what hard rule 4 says peak must not be.

    Wide rows are the case that separates the two, so this builds them: 50
    generators summed into each of 4 snapshots is 50 entries per row.
    """
    n_g, n_s = 50, 4
    gens = pd.DataFrame(
        {
            'generator': [f'g{i}' for i in range(n_g)],
            'p_max': np.full(n_g, 10.0),
            'cost': np.arange(1.0, n_g + 1.0),
        }
    )
    load = pd.DataFrame({'snapshot': np.arange(n_s), 'value': np.full(n_s, 100.0)})

    with DuckdbExecutor(memory_limit='256MB') as ex:
        ex.build(dispatch_program(), dispatch_sources(gens, load))
        tables = ex._tables()
        assert tables.scalar('SELECT count(*) FROM A') == n_g * n_s

        def widest(ranges):
            return max(tables.scalar(f'SELECT count(*) FROM A WHERE row >= {lo} AND row < {hi}') for lo, hi in ranges)

        budget = 100
        bounded = list(tables.row_chunks_by_nonzeros(budget))
        assert widest(bounded) <= budget

        # the same budget spent as if a row cost one element puts every entry
        # in one chunk — 2x the budget here, and unbounded in general, because
        # nothing caps how wide a row gets
        assert widest(list(chunking.ranges(tables.row_count, budget, 1.0))) == n_g * n_s


@pytest.mark.parametrize(
    ('total', 'budget', 'width'),
    [(0, 100, 1.0), (1, 100, 1.0), (10, 3, 1.0), (10, 100, 1.0), (10, 3, 7.0), (10, 3, 0.25), (10, 1, 1e9)],
)
def test_chunk_ranges_are_contiguous_gapless_and_cover_everything(total, budget, width):
    """The property the whole hand-off rests on, at every awkward size.

    ``addCols`` and ``addRows`` append: column *k* must be the *k*-th row handed
    over. That holds only if the ranges are ordered, consecutive and gapless —
    a dropped range silently shortens the model, an overlapping one relabels
    it, and neither shows up as an error. The widths here include one below 1
    and one far above the budget, the two ends where a ``//`` can produce a
    zero step or a chunk wider than asked for.
    """
    got = list(chunking.ranges(total, budget, width))

    assert all(lo < hi for lo, hi in got), 'an empty range means a wasted pass'
    assert [lo for lo, _ in got] == sorted(lo for lo, _ in got), 'ranges must ascend'
    assert [i for lo, hi in got for i in range(lo, hi)] == list(range(total)), 'gap, overlap, or short'
    if total:
        widest = max(hi - lo for lo, hi in got)
        assert widest * max(1.0, width) <= max(budget, max(1.0, width)), 'a chunk exceeded the budget'
