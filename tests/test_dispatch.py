"""Integration test: the dispatch example from the spec."""

import pandas as pd

import linopy_yaml as ly
from tests.oracle import compat


def test_dispatch_builds(dispatch_yaml):
    """Build the dispatch model from YAML and verify structure."""
    m = compat.build(
        dispatch_yaml,
        data={
            'p_max': pd.Series({'wind': 100, 'solar': 60, 'gas': 200}),
            'load': pd.Series(
                [80, 120, 150, 180, 140, 100],
                index=pd.RangeIndex(6, name='snapshot'),
            ),
            'cost': pd.Series({'wind': 0, 'solar': 0, 'gas': 50}),
        },
        coords={
            'snapshot': pd.RangeIndex(6, name='snapshot'),
        },
    )

    # Variables
    assert 'p' in m.variables

    # Constraints
    assert 'power_balance' in m.constraints

    # Objective was set
    assert m.objective is not None

    # The model stands for itself; the schema is re-read from the file when
    # wanted, never carried on the model (compat is a pure producer).
    schema = ly.load_schema(dispatch_yaml)
    assert schema.variables['p'].foreach == ['snapshot', 'generator']
    assert schema.parameters['load'].dims == ['snapshot']


def test_dispatch_solves(dispatch_yaml):
    """Build and solve the dispatch model, check solution is feasible."""
    m = compat.build(
        dispatch_yaml,
        data={
            'p_max': pd.Series({'wind': 100, 'solar': 60, 'gas': 200}),
            'load': pd.Series(
                [80, 120, 150, 180, 140, 100],
                index=pd.RangeIndex(6, name='snapshot'),
            ),
            'cost': pd.Series({'wind': 0, 'solar': 0, 'gas': 50}),
        },
        coords={
            'snapshot': pd.RangeIndex(6, name='snapshot'),
        },
    )

    status = m.solve(solver_name='highs')
    assert status[0] == 'ok'

    # Check solution: all generation non-negative
    p_sol = m.solution['p']
    assert (p_sol >= -1e-6).all()

    # Check power balance is satisfied
    for t in range(6):
        load_t = [80, 120, 150, 180, 140, 100][t]
        gen_sum = float(p_sol.sel(snapshot=t).sum())
        assert abs(gen_sum - load_t) < 1e-4, f'Balance violated at t={t}'
