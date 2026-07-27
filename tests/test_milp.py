"""vtype: a unit-commitment MILP through both backends.

Binary commitment variables u with p <= p_max * u and a fixed commitment
cost. Verifies the relational backend's vtype path end to end: cols vtype
column, HiGHS changeColsIntegrality in solver_direct, and the LP binary
section.
"""

from __future__ import annotations

import numpy as np

from tests.differential import differential

COMMITMENT_YAML = """
dimensions:
  snapshot: {dtype: int}
  generator: {dtype: str}

parameters:
  p_max: {dims: [generator]}
  cost: {dims: [generator]}
  fix_cost: {dims: [generator]}
  load: {dims: [snapshot]}

variables:
  u:
    foreach: [snapshot, generator]
    binary: true
  p:
    foreach: [snapshot, generator]
    bounds: {lower: 0, upper: p_max}

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


def test_commitment_milp_agrees_and_stays_integral(commitment_inputs):
    data, coords = commitment_inputs

    with differential(COMMITMENT_YAML, data, coords, lp=True) as run:
        # commitment must actually bind somewhere (u not all-1 at the optimum)
        assert float(run.model.solution['u'].sum()) < run.model.solution['u'].size

        # binary variables actually take integral 0/1 values
        u = run.result.primal('u')['value'].to_numpy()
        assert np.allclose(u, np.round(u), atol=1e-6)
        assert set(np.round(u)) <= {0.0, 1.0}

        # the LP file carries integrality, not just bounds
        assert 'binary' in run.lp.read_text()
