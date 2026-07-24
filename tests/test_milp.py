"""vtype: a unit-commitment MILP through both backends.

Binary commitment variables u with p <= p_max * u and a fixed commitment
cost. Verifies the relational backend's vtype path end to end: cols vtype
column, HiGHS changeColsIntegrality in solver_direct, and the LP binary
section.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

duckdb = pytest.importorskip('duckdb')
highspy = pytest.importorskip('highspy')

import yaml as pyyaml  # noqa: E402

from linopy_yaml import compat  # noqa: E402
from linopy_yaml.lowering import lower_program, tidy_sources  # noqa: E402
from linopy_yaml.relational import DuckdbExecutor  # noqa: E402
from linopy_yaml.schema import MathSchema  # noqa: E402

RTOL = 1e-9

COMMITMENT_YAML = """
dimensions:
  snapshot:
    dtype: int
  generator:
    dtype: str

parameters:
  p_max:
    dims: [generator]
  cost:
    dims: [generator]
  fix_cost:
    dims: [generator]
  load:
    dims: [snapshot]

variables:
  u:
    foreach: [snapshot, generator]
    binary: true
  p:
    foreach: [snapshot, generator]
    bounds:
      lower: 0
      upper: p_max

constraints:
  commitment:
    foreach: [snapshot, generator]
    equations:
      - expression: p <= p_max * u
  balance:
    foreach: [snapshot]
    equations:
      - expression: sum(p, over=generator) == load

objectives:
  total_cost:
    sense: minimize
    equations:
      - expression: sum(p * cost, over=generator) + sum(u * fix_cost, over=generator)
"""


@pytest.fixture
def commitment_inputs(tmp_path):
    rng = np.random.default_rng(5)
    n_s = 24
    p_max = pd.Series({'coal': 120.0, 'gas': 80.0, 'peaker': 60.0})
    cost = pd.Series({'coal': 10.0, 'gas': 30.0, 'peaker': 90.0})
    fix_cost = pd.Series({'coal': 400.0, 'gas': 150.0, 'peaker': 20.0})
    load = pd.Series(
        (rng.uniform(0.3, 0.9, n_s) * p_max.sum()).round(1),
        index=pd.RangeIndex(n_s, name='snapshot'),
    )
    data = {'p_max': p_max, 'cost': cost, 'fix_cost': fix_cost, 'load': load}
    coords = {
        'snapshot': pd.RangeIndex(n_s, name='snapshot'),
        'generator': pd.Index(p_max.index, name='generator'),
    }
    yaml_path = tmp_path / 'commitment.yaml'
    yaml_path.write_text(COMMITMENT_YAML)
    return yaml_path, data, coords


def test_commitment_milp_differential(commitment_inputs, tmp_path):
    yaml_path, data, coords = commitment_inputs

    m = compat.build(yaml_path, data=data, coords=coords)
    m.solve(solver_name='highs', output_flag=False)
    oracle = float(m.objective.value)
    assert np.isfinite(oracle)
    # commitment must actually bind somewhere (u not all-1 at the optimum)
    assert float(m.solution['u'].sum()) < m.solution['u'].size

    schema = MathSchema(**pyyaml.safe_load(COMMITMENT_YAML))
    with DuckdbExecutor(memory_limit='256MB') as ex:
        ex.build(lower_program(schema), tidy_sources(schema, data, coords))

        sol = ex.solve()
        assert sol.status == 'Optimal'
        assert sol.objective == pytest.approx(oracle, rel=RTOL)

        # binary variables actually take integral 0/1 values
        u = sol.primal('u')['value'].to_numpy()
        assert np.allclose(u, np.round(u), atol=1e-6)
        assert set(np.round(u)) <= {0.0, 1.0}

        lp = tmp_path / 'commitment.lp'
        ex.write_lp(lp)
        h = highspy.Highs()
        h.setOptionValue('output_flag', False)
        h.readModel(str(lp))
        h.run()
        assert h.getInfo().objective_function_value == pytest.approx(oracle, rel=RTOL)
        # the LP file carries integrality, not just bounds
        assert 'binary' in lp.read_text()
