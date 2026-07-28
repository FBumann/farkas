"""vtype: a unit-commitment MILP through both backends.

Binary commitment variables u with p <= p_max * u and a fixed commitment
cost. Verifies the relational backend's vtype path end to end: cols vtype
column, HiGHS changeColsIntegrality in solver_direct, and the LP binary
section.
"""

from __future__ import annotations

from typing import get_args

import numpy as np
import pytest

from farkas.relational import plan
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


@pytest.mark.parametrize('batch_rows', [7, 13, 100_000], ids=['tiny-chunks', 'odd-chunks', 'one-chunk'])
def test_solver_direct_ingests_columns_in_order_whatever_the_chunking(commitment_inputs, batch_rows):
    """Columns reach HiGHS in label order however the range loop splits them.

    ``addCols`` appends, so column *k* must be the *k*-th row handed over. The
    sink used to get that from one ``ORDER BY c.col`` over the whole table — a
    global sort, which is the operator that does not stay inside
    ``memory_limit``. It now walks bounded ``col_chunks`` instead, which is
    only equivalent if every chunk is ordered *and* the chunks themselves are
    consecutive and gapless.

    A binary model is the sharp case: integrality is applied by column index,
    so a chunking bug relabels which variables are integral and the objective
    moves. Prime batch sizes make the last chunk short and stop a bug that
    only shows on ragged splits from hiding behind a round number.
    """
    data, coords = commitment_inputs

    with differential(COMMITMENT_YAML, data, coords) as run:
        oracle = run.oracle
        chunked = run.executor.solve(batch_rows=batch_rows)
        assert chunked.is_ok
        assert chunked.objective == pytest.approx(oracle, rel=1e-9)

        u = chunked.primal('u')['value'].to_numpy()
        assert set(np.round(u)) <= {0.0, 1.0}, 'integrality landed on the wrong columns'


def test_cols_vtype_enum_covers_every_declared_variable_type(commitment_inputs):
    """``cols.vtype`` is an ENUM, and its members are ``plan.VariableType``.

    The storage choice is a performance one — three literals repeated once per
    column are the widest thing on the row — but the *members* are a contract.
    An ENUM rejects a value it does not know, so a fourth variable type added
    to the plan and not to the table would fail at insert with a duckdb error
    about a cast, a long way from the declaration that caused it. This is the
    test that fails at the declaration instead.
    """
    data, coords = commitment_inputs

    with differential(COMMITMENT_YAML, data, coords) as run:
        con = run.executor._con
        column_type = con.execute("SELECT column_type FROM (DESCRIBE cols) WHERE column_name = 'vtype'").fetchone()
        assert column_type is not None
        members = con.execute('SELECT unnest(enum_range(NULL::variable_type))').fetchall()

    assert column_type[0] != 'VARCHAR'
    assert {m[0] for m in members} == set(get_args(plan.VariableType))
