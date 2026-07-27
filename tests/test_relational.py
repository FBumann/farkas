"""Phase-2 gate: two real models round-trip through solve on the relational backend.

Each model is built three ways and must agree on the objective:
  1. relational executor -> solver_direct (HiGHS via batched addCols/addRows)
  2. relational executor -> lp_file sink -> HiGHS reads and solves the file
  3. eager linopy build (the correctness oracle)
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import polars as pl
import pytest

import farkas as fk
from farkas.errors import DataError, LanguageError
from farkas.lowering import lower_program
from farkas.relational import (
    PolarsExecutor,
)
from farkas.relational.plan import (
    Constant,
    ConstraintDeclaration,
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
from farkas.schema import MathSchema
from tests.conftest import solve_lp_file
from tests.differential import RTOL
from tests.oracle import linopy, pd, transport_eager_objective, xr

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

    with PolarsExecutor() as ex:
        ex.build(dispatch_program(), dispatch_sources(gens, load))

        result = ex.solve()
        assert result.is_ok
        assert result.objective == pytest.approx(oracle, rel=RTOL)

        lp = tmp_path / 'dispatch.lp'
        ex.write_lp(lp)
        assert solve_lp_file(lp) == pytest.approx(oracle, rel=RTOL)

        # masked variable rows are absent, and primal joins back to coords
        primal = result.to_pandas('p')
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

    with PolarsExecutor() as ex:
        ex.build(transport_program(), transport_sources(gens, lines, load))

        result = ex.solve()
        assert result.is_ok
        assert result.objective == pytest.approx(oracle, rel=RTOL)

        lp = tmp_path / 'transport.lp'
        ex.write_lp(lp)
        assert solve_lp_file(lp) == pytest.approx(oracle, rel=RTOL)

        # flows respect line capacity bounds
        primal_f = result.to_pandas('f')
        caps = lines.set_index('line')['cap']
        limits = primal_f['line'].map(caps)
        assert (primal_f['value'].abs() <= limits + 1e-6).all()


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
    with PolarsExecutor() as ex, pytest.raises(LanguageError, match='nonlinear'):
        ex.build(bad, dispatch_sources(gens, load))


def test_missing_source_rejected(dispatch_data):
    gens, load = dispatch_data
    sources = dispatch_sources(gens, load)
    del sources['cost']
    with PolarsExecutor() as ex, pytest.raises(DataError, match="no source bound for parameter 'cost'"):
        ex.build(dispatch_program(), sources)


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
    with PolarsExecutor() as ex, pytest.raises(LanguageError, match='missing a Sum'):
        ex.build(bad, dispatch_sources(gens, load))


def test_an_awkward_path_is_a_value_not_syntax(tmp_path):
    """Paths come from the calling program, so no language rule constrains them.

    ``o'brien`` is a legal directory name, and a quote in one must be as
    uninteresting as a quote in a label. Every path-carrying sink and source is
    exercised here: a parquet source, an explicit index source, the LP writer,
    and the parquet sink.
    """
    odd = tmp_path / "o'brien"
    odd.mkdir()
    pl.DataFrame({'snapshot': [0, 1], 'value': [1.0, 2.0]}).write_parquet(odd / 'load.parquet')
    pl.DataFrame({'snapshot': [0, 1]}).write_parquet(odd / 'index.parquet')

    model = {
        'dimensions': {'snapshot': {'dtype': 'int'}},
        'parameters': {'load': {'dims': ['snapshot']}},
        'variables': {'p': {'foreach': ['snapshot'], 'bounds': {'lower': 0}}},
        'constraints': {'meet': {'foreach': ['snapshot'], 'equations': [{'expression': 'p >= load'}]}},
        'objectives': {'c': {'sense': 'minimize', 'equations': [{'expression': 'sum(p, over=snapshot)'}]}},
    }
    sources = {'load': str(odd / 'load.parquet'), 'snapshot': str(odd / 'index.parquet')}

    fk.write(model, sources, odd / 'model.lp')
    result = fk.solve(model, sources)
    assert result.objective == pytest.approx(3.0)
    assert set(result.to_parquet(odd / 'solution')) == {'p'}


def test_a_variable_appearing_twice_in_a_row_is_summed_not_duplicated():
    """The case the skipped aggregate must not break.

    `x + 2 * x` is two term fragments landing on one solver column, so the
    assembly has to add them. Its coefficient must be 3, and the row must hold
    one entry for that column rather than two — a solver handed the same
    column twice in one row is entitled to reject the model.
    """
    model = {
        'dimensions': {'i': {'dtype': 'int', 'values': [0, 1]}},
        'parameters': {'rhs': {'dims': ['i']}},
        'variables': {'x': {'foreach': ['i'], 'bounds': {'lower': 0}}},
        'constraints': {'c': {'foreach': ['i'], 'equations': [{'expression': 'x + 2 * x >= rhs'}]}},
        'objectives': {'o': {'sense': 'minimize', 'equations': [{'expression': 'sum(x, over=i)'}]}},
    }
    sources = {'rhs': pl.DataFrame({'i': [0, 1], 'value': [6.0, 9.0]})}
    with fk.build(model, sources) as ex:
        matrix = ex._tables().matrix
        assert matrix.height == 2  # one entry per row, not one per fragment
        assert sorted(matrix['coeff'].to_list()) == [3.0, 3.0]
        result = ex.solve()
    assert result.objective == pytest.approx(5.0)  # 6/3 + 9/3


def test_an_objective_naming_a_variable_twice_sums_its_coefficients():
    """Same argument, one dimension down: the objective is a column vector."""
    model = {
        'dimensions': {'i': {'dtype': 'int', 'values': [0]}},
        'parameters': {'lb': {'dims': ['i']}},
        'variables': {'x': {'foreach': ['i'], 'bounds': {'lower': 'lb'}}},
        'constraints': {'c': {'foreach': ['i'], 'equations': [{'expression': 'x >= lb'}]}},
        'objectives': {'o': {'sense': 'minimize', 'equations': [{'expression': 'x + 4 * x'}]}},
    }
    with fk.build(model, {'lb': pl.DataFrame({'i': [0], 'value': [2.0]})}) as ex:
        assert ex._tables().obj.height == 1
        assert ex._tables().obj['coeff'].to_list() == [5.0]
        assert ex.solve().objective == pytest.approx(10.0)


def test_a_mask_that_removes_nothing_labels_exactly_like_no_mask(dispatch_data):
    """A vacuous `where` must not shift a single solver index.

    Labels are the solver's own column numbers, so "the mask removed nothing"
    and "there was no mask" have to produce identical frames rather than merely
    the same row count.
    """
    gens, load = dispatch_data
    gens = gens.assign(p_max=gens['p_max'].where(gens['p_max'] > 0, 1.0))  # nothing left to mask out

    labels = []
    for where in (None, ParameterComparison('p_max', '>', 0)):
        base = dispatch_program()
        program = replace(base, variables=(replace(base.variables[0], where=where),))
        with PolarsExecutor() as ex:
            ex.build(program, dispatch_sources(gens, load))
            labels.append(ex._variables['p'].collect().sort('var_label'))
    assert labels[0].equals(labels[1])


def _objective_of(program, sources):
    """`obj` as `{col: coeff}`, plus whether the aggregate was skipped."""
    with PolarsExecutor() as ex:
        ex.build(program, sources)
        obj = ex._tables().obj
        return dict(zip(obj['col'].to_list(), obj['coeff'].to_list(), strict=True)), obj.height


def test_the_objective_skips_the_aggregate_only_when_a_column_cannot_repeat():
    """`p * cost` needs no aggregate; `p * cost + p * cost` does."""
    base = {
        'dimensions': {'i': {'dtype': 'int', 'values': [0, 1]}},
        'parameters': {'cost': {'dims': ['i']}, 'lb': {'dims': ['i']}},
        'variables': {'p': {'foreach': ['i'], 'bounds': {'lower': 'lb'}}},
        'constraints': {'c': {'foreach': ['i'], 'equations': [{'expression': 'p >= lb'}]}},
    }
    sources = {
        'cost': pl.DataFrame({'i': [0, 1], 'value': [2.0, 3.0]}),
        'lb': pl.DataFrame({'i': [0, 1], 'value': [1.0, 1.0]}),
    }
    once = lower_program(
        MathSchema(**dict(base, objectives={'o': {'sense': 'minimize', 'equations': [{'expression': 'p * cost'}]}}))
    )
    twice = lower_program(
        MathSchema(
            **dict(base, objectives={'o': {'sense': 'minimize', 'equations': [{'expression': 'p * cost + p * cost'}]}})
        )
    )

    assert _objective_of(once, sources) == ({0: 2.0, 1: 3.0}, 2)
    assert _objective_of(twice, sources) == ({0: 4.0, 1: 6.0}, 2)


def test_the_objective_keeps_the_aggregate_when_a_reduction_hides_extra_rows():
    """A fragment's dims can match the variable's while its rows do not.

    `sum(q * price, over=generator)` with `q` indexed by snapshot alone reduces
    to dims `('snapshot',)` — exactly `q`'s declaration — but `_sum_fragment`
    *projects*, so the fragment still carries one row per generator. Skipping
    the aggregate there writes |generator| rows into `obj` for a single column:
    the LP file would quietly re-sum them, while `cols` joined to `obj` in the
    HiGHS sink would hand the solver more columns than the model has.

    This is the case a dims-equality test gets wrong, and the reason a fragment
    tracks which of its dims its label actually determines.
    """
    model = {
        'dimensions': {'snapshot': {'dtype': 'int', 'values': [0, 1]}, 'generator': {'values': ['g0', 'g1', 'g2']}},
        'parameters': {'price': {'dims': ['snapshot', 'generator']}, 'load': {'dims': ['snapshot']}},
        'variables': {'q': {'foreach': ['snapshot'], 'bounds': {'lower': 0, 'upper': 10}}},
        'constraints': {'floor': {'foreach': ['snapshot'], 'equations': [{'expression': 'q >= load'}]}},
        'objectives': {'o': {'sense': 'minimize', 'equations': [{'expression': 'sum(q * price, over=generator)'}]}},
    }
    sources = {
        'price': pl.DataFrame(
            {'snapshot': [0, 0, 0, 1, 1, 1], 'generator': ['g0', 'g1', 'g2'] * 2, 'value': [1.0, 2.0, 3.0] * 2}
        ),
        'load': pl.DataFrame({'snapshot': [0, 1], 'value': [5.0, 5.0]}),
    }
    # one row per column, each carrying the summed price — not three rows of one
    assert _objective_of(lower_program(MathSchema(**model)), sources) == ({0: 6.0, 1: 6.0}, 2)


def test_infinite_bounds_survive_the_handoff(dispatch_data):
    """An absent upper bound must reach HiGHS as infinity, not as a number."""
    gens, load = dispatch_data
    base = dispatch_program()
    unbounded = replace(base, variables=(replace(base.variables[0], upper=Constant(float('inf'))),))
    with PolarsExecutor() as ex:
        ex.build(unbounded, dispatch_sources(gens, load))
        assert ex._tables().cols['ub'].is_infinite().all()
        assert ex.solve().is_ok
