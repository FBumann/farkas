"""Phase-2 gate: two real models round-trip through solve on the relational backend.

Each model is built three ways and must agree on the objective:
  1. relational executor -> solver_direct (HiGHS via batched addCols/addRows)
  2. relational executor -> lp_file sink -> HiGHS reads and solves the file
  3. eager linopy build (the correctness oracle)
"""

from __future__ import annotations

import highspy
import numpy as np
import pandas as pd
import pytest

import farkas as fk
from farkas.errors import DataError, LanguageError
from farkas.relational import (
    DuckdbExecutor,
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
from tests.oracle import linopy, transport_eager_objective, xr

RTOL = 1e-9


def solve_lp_file(path) -> float:
    h = highspy.Highs()
    h.setOptionValue('output_flag', False)
    h.readModel(str(path))
    h.run()
    assert h.getModelStatus() == highspy.HighsModelStatus.kOptimal
    return h.getInfo().objective_function_value


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

        sol = ex.solve()
        assert sol.status == 'Optimal'
        assert sol.objective == pytest.approx(oracle, rel=RTOL)

        lp = tmp_path / 'dispatch.lp'
        ex.write_lp(lp)
        assert solve_lp_file(lp) == pytest.approx(oracle, rel=RTOL)

        # masked variable rows are absent, and primal joins back to coords
        primal = sol.primal('p')
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

        sol = ex.solve()
        assert sol.status == 'Optimal'
        assert sol.objective == pytest.approx(oracle, rel=RTOL)

        lp = tmp_path / 'transport.lp'
        ex.write_lp(lp)
        assert solve_lp_file(lp) == pytest.approx(oracle, rel=RTOL)

        # flows respect line capacity bounds
        primal_f = sol.primal('f')
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
    with DuckdbExecutor() as ex, pytest.raises(LanguageError, match='nonlinear'):
        ex.build(bad, dispatch_sources(gens, load))


def test_missing_source_rejected(dispatch_data):
    gens, load = dispatch_data
    sources = dispatch_sources(gens, load)
    del sources['cost']
    with DuckdbExecutor() as ex, pytest.raises(DataError, match="no source bound for parameter 'cost'"):
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
    with DuckdbExecutor() as ex, pytest.raises(LanguageError, match='missing a Sum'):
        ex.build(bad, dispatch_sources(gens, load))


# --------------------------------------------------------------------------
# the objective's GROUP BY is skipped only where it is provably dead weight
# --------------------------------------------------------------------------


def _objective_model(expression: str) -> dict:
    return {
        'dimensions': {'snapshot': {'dtype': 'int'}, 'generator': {'dtype': 'str'}},
        'parameters': {
            'p_max': {'dims': ['generator']},
            'cost': {'dims': ['generator']},
            'cost2': {'dims': ['generator']},
            'load': {'dims': ['snapshot']},
        },
        'variables': {'p': {'foreach': ['snapshot', 'generator'], 'bounds': {'lower': 0, 'upper': 'p_max'}}},
        'constraints': {
            'balance': {
                'foreach': ['snapshot'],
                'equations': [{'expression': 'sum(p, over=generator) == load'}],
            }
        },
        'objectives': {'total': {'sense': 'minimize', 'equations': [{'expression': expression}]}},
    }


def _objective_sources():
    return {
        'p_max': pd.DataFrame({'generator': ['w', 's'], 'value': [100.0, 60.0]}),
        'cost': pd.DataFrame({'generator': ['w', 's'], 'value': [1.0, 2.0]}),
        'cost2': pd.DataFrame({'generator': ['w', 's'], 'value': [3.0, 4.0]}),
        'load': pd.DataFrame({'snapshot': [0, 1], 'value': [50.0, 70.0]}),
    }


@pytest.mark.parametrize(
    ('expression', 'skips_aggregate', 'objective'),
    [
        # one fragment, one variable, fragment dims == variable dims
        ('p * cost', True, 120.0),
        # two fragments land on the same column: the sum is the objective
        ('p * cost + p * cost2', False, 480.0),
        # the sum drops a dim, so the fragment's dims are not the variable's
        ('sum(p, over=generator)', False, 120.0),
        # Divide spells its operands numerator/divisor, not left/right — a
        # walker that assumes otherwise raises instead of matching
        ('p / cost', True, 65.0),
    ],
)
def test_objective_aggregate_skipped_only_when_columns_are_unique(monkeypatch, expression, skips_aggregate, objective):
    """Skipping ``GROUP BY col`` is only sound when no column can repeat.

    The value assertions are the real guard: were the fast path taken for a
    shape that does repeat a column, the objective would silently lose terms
    rather than fail.
    """
    seen: list[bool] = []
    original = DuckdbExecutor._objective_is_one_row_per_column

    def spy(self, expr, comp):
        seen.append(original(self, expr, comp))
        return seen[-1]

    monkeypatch.setattr(DuckdbExecutor, '_objective_is_one_row_per_column', spy)

    with fk.solve(_objective_model(expression), _objective_sources()) as solution:
        assert solution.status == 'Optimal'
        assert solution.objective == pytest.approx(objective)

    assert seen == [skips_aggregate]
