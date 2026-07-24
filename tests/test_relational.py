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

from linopy_yaml.relational import (
    DuckdbExecutor,
    RelationalBuildError,
)
from linopy_yaml.relational.ir import (
    Cmp,
    Const,
    ConstraintDecl,
    GroupSum,
    ObjectiveDecl,
    Param,
    ParameterDecl,
    Program,
    Sum,
    Var,
    VariableDecl,
)
from tests.oracle import linopy, xr

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
            ParameterDecl('p_max', ('generator',)),
            ParameterDecl('cost', ('generator',)),
            ParameterDecl('load', ('snapshot',)),
        ),
        variables=(
            VariableDecl(
                'p',
                ('snapshot', 'generator'),
                where=Cmp('p_max', '>', 0),
                lower=Const(0.0),
                upper=Param('p_max'),
            ),
        ),
        constraints=(
            ConstraintDecl(
                'power_balance',
                ('snapshot',),
                lhs=Sum(Var('p'), over=('generator',)),
                sense='==',
                rhs=Param('load'),
            ),
        ),
        objective=ObjectiveDecl('min', Sum(Var('p') * Param('cost'), over=('generator', 'snapshot'))),
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


@pytest.fixture
def transport_data():
    rng = np.random.default_rng(11)
    n_s, n_b, n_g, n_l = 24, 4, 9, 5
    buses = [f'b{i}' for i in range(n_b)]
    gens = pd.DataFrame(
        {
            'generator': [f'g{i}' for i in range(n_g)],
            # round-robin so every bus has local generation (keeps the data
            # feasible); the cost spread still makes cross-bus flows optimal
            'bus': [buses[i % n_b] for i in range(n_g)],
            'p_max': rng.uniform(80, 150, n_g).round(3),
            'cost': rng.uniform(5, 100, n_g).round(3),
        }
    )
    # ring topology plus one chord so every bus is reachable
    pairs = [(buses[i], buses[(i + 1) % n_b]) for i in range(n_b)] + [(buses[0], buses[2])]
    lines = pd.DataFrame(
        {
            'line': [f'l{i}' for i in range(n_l)],
            'from_bus': [a for a, _ in pairs],
            'to_bus': [b for _, b in pairs],
            'cap': rng.uniform(60, 120, n_l).round(3),
        }
    )
    # loads below each bus's local capacity — feasible even with zero flow
    local_cap = gens.groupby('bus')['p_max'].sum().reindex(buses).to_numpy()
    factors = rng.uniform(0.3, 0.8, (n_s, n_b))
    load = pd.DataFrame(
        {
            'snapshot': np.repeat(np.arange(n_s), n_b),
            'bus': buses * n_s,
            'value': (factors * local_cap).round(3).ravel(),
        }
    )
    return gens, lines, load


def transport_program() -> Program:
    injection = (
        GroupSum(Var('p'), mapping='gen_bus', into='bus')
        + GroupSum(Var('f'), mapping='line_to', into='bus')
        - GroupSum(Var('f'), mapping='line_from', into='bus')
    )
    return Program(
        parameters=(
            ParameterDecl('p_max', ('generator',)),
            ParameterDecl('cost', ('generator',)),
            ParameterDecl('gen_bus', ('generator',)),
            ParameterDecl('cap', ('line',)),
            ParameterDecl('line_from', ('line',)),
            ParameterDecl('line_to', ('line',)),
            ParameterDecl('load', ('snapshot', 'bus')),
        ),
        variables=(
            VariableDecl(
                'p',
                ('snapshot', 'generator'),
                lower=Const(0.0),
                upper=Param('p_max'),
            ),
            VariableDecl(
                'f',
                ('snapshot', 'line'),
                lower=-Param('cap'),
                upper=Param('cap'),
            ),
        ),
        constraints=(
            ConstraintDecl(
                'balance',
                ('snapshot', 'bus'),
                lhs=injection,
                sense='==',
                rhs=Param('load'),
            ),
        ),
        objective=ObjectiveDecl('min', Sum(Var('p') * Param('cost'), over=('generator', 'snapshot'))),
    )


def transport_sources(gens, lines, load) -> dict:
    return {
        'p_max': gens[['generator', 'p_max']].rename(columns={'p_max': 'value'}),
        'cost': gens[['generator', 'cost']].rename(columns={'cost': 'value'}),
        'gen_bus': gens[['generator', 'bus']].rename(columns={'bus': 'value'}),
        'cap': lines[['line', 'cap']].rename(columns={'cap': 'value'}),
        'line_from': lines[['line', 'from_bus']].rename(columns={'from_bus': 'value'}),
        'line_to': lines[['line', 'to_bus']].rename(columns={'to_bus': 'value'}),
        'load': load,
        'snapshot': load[['snapshot']],
        'bus': load[['bus']],
    }


def transport_eager_objective(gens, lines, load) -> float:
    gi = gens.set_index('generator')
    li = lines.set_index('line')
    snapshots = pd.Index(sorted(load['snapshot'].unique()), name='snapshot')
    buses = pd.Index(sorted(load['bus'].unique()), name='bus')

    load_da = xr.DataArray.from_series(load.set_index(['snapshot', 'bus'])['value'])
    p_max = xr.DataArray.from_series(gi['p_max'])
    cost = xr.DataArray.from_series(gi['cost'])
    cap = xr.DataArray.from_series(li['cap'])

    gen_at = xr.DataArray(
        (gi['bus'].to_numpy()[None, :] == buses.to_numpy()[:, None]).astype(float),
        coords={'bus': buses, 'generator': gi.index},
        dims=['bus', 'generator'],
    )
    line_in = xr.DataArray(
        (li['to_bus'].to_numpy()[None, :] == buses.to_numpy()[:, None]).astype(float),
        coords={'bus': buses, 'line': li.index},
        dims=['bus', 'line'],
    )
    line_out = xr.DataArray(
        (li['from_bus'].to_numpy()[None, :] == buses.to_numpy()[:, None]).astype(float),
        coords={'bus': buses, 'line': li.index},
        dims=['bus', 'line'],
    )

    m = linopy.Model()
    p = m.add_variables(lower=0, upper=p_max, coords=[snapshots, gi.index], name='p')
    f = m.add_variables(lower=-cap, upper=cap, coords=[snapshots, li.index], name='f')
    injection = (p * gen_at).sum('generator') + (f * line_in).sum('line') - (f * line_out).sum('line')
    m.add_constraints(injection == load_da, name='balance')
    m.add_objective((p * cost).sum())
    m.solve(solver_name='highs', output_flag=False)
    return float(m.objective.value)


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
        objective=ObjectiveDecl('min', Sum(Var('p') * Var('p'), over=('generator', 'snapshot'))),
    )
    with DuckdbExecutor() as ex, pytest.raises(RelationalBuildError, match='nonlinear'):
        ex.build(bad, dispatch_sources(gens, load))


def test_missing_source_rejected(dispatch_data):
    gens, load = dispatch_data
    sources = dispatch_sources(gens, load)
    del sources['cost']
    with DuckdbExecutor() as ex, pytest.raises(RelationalBuildError, match="no source bound for parameter 'cost'"):
        ex.build(dispatch_program(), sources)


def test_out_of_foreach_dims_rejected(dispatch_data):
    gens, load = dispatch_data
    prog = dispatch_program()
    bad = Program(
        parameters=prog.parameters,
        variables=prog.variables,
        constraints=(
            ConstraintDecl(
                'power_balance',
                ('snapshot',),
                lhs=Var('p'),  # generator dim not summed
                sense='==',
                rhs=Param('load'),
            ),
        ),
        objective=prog.objective,
    )
    with DuckdbExecutor() as ex, pytest.raises(RelationalBuildError, match='missing a Sum'):
        ex.build(bad, dispatch_sources(gens, load))
