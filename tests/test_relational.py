"""Phase-2 gate: two real models round-trip through solve on the relational backend.

Each model is built three ways and must agree on the objective:
  1. relational executor -> solver_direct (HiGHS via batched addCols/addRows)
  2. relational executor -> lp_file sink -> HiGHS reads and solves the file
  3. eager linopy build (the correctness oracle)
"""

from __future__ import annotations

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
