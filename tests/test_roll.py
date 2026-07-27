"""roll / shift: time-coupled recurrences through both backends.

examples/storage.yaml is dispatch plus a cyclic battery:
soc == roll(soc, snapshot=1) + charge * 0.9 - discharge. The eager backend
implements roll with linopy's circular .roll(); the relational backend lowers
it to plan.Shift — a pointwise ord-join remap.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from farkas.errors import LanguageError
from farkas.lowering import _lower_expr
from farkas.relational.plan import (
    Translate,
    Variable,
)
from tests.conftest import resolved, schema_of
from tests.differential import differential
from tests.oracle import pd

STORAGE_YAML = Path('examples/storage.yaml')


@pytest.fixture
def storage_inputs():
    """Peaky load that exceeds generation capacity at the peaks, so the
    battery is *required* (not just economic) and soc is genuinely coupled."""
    n_s = 48
    p_max = pd.Series({'wind': 80.0, 'gas': 70.0})
    cost = pd.Series({'wind': 1.0, 'gas': 40.0})
    t = np.arange(n_s)
    load = pd.Series(
        (110 + 60 * np.sin(2 * np.pi * t / 24)).round(3),  # peaks at 170 > 150
        index=pd.RangeIndex(n_s, name='snapshot'),
    )
    data = {'p_max': p_max, 'cost': cost, 'load': load}
    coords = {
        'snapshot': pd.RangeIndex(n_s, name='snapshot'),
        'generator': pd.Index(p_max.index, name='generator'),
    }
    return data, coords


def _soc_trace(result):
    """(soc, prev-contribution inputs) as plain arrays, sorted by snapshot."""
    return tuple(
        result.to_pandas(name).set_index('snapshot')['value'].sort_index().to_numpy()
        for name in ('soc', 'charge', 'discharge')
    )


# ---------------------------------------------------------------------------
# the recurrence, end to end
# ---------------------------------------------------------------------------


def test_roll_is_cyclic_on_both_lanes(storage_inputs):
    data, coords = storage_inputs

    with differential(STORAGE_YAML, data, coords, lp=True) as run:
        # the battery must actually cycle for the model to be feasible
        assert float(run.model.solution['discharge'].max()) > 1e-3

        soc, charge, discharge = _soc_trace(run.result)
        assert np.allclose(soc, np.roll(soc, 1) + 0.9 * charge - discharge, atol=1e-6)


def test_shift_is_acyclic_on_both_lanes(storage_inputs):
    """shift() = acyclic recurrence: soc starts empty instead of wrapping.

    The load is scaled down vs the cyclic test: starting empty, the battery
    can only pre-charge from t=0, so the first peak must be shallower (with
    the original data both backends agree the model is infeasible).
    """
    data, coords = storage_inputs
    data = {**data, 'load': (data['load'] * 0.93).round(3)}

    original = STORAGE_YAML.read_text()
    assert 'roll(soc, snapshot=1)' in original
    acyclic = original.replace('roll(soc, snapshot=1)', 'shift(soc, snapshot=1)')

    with differential(acyclic, data, coords) as run:
        # acyclic recurrence: soc[0] has no predecessor (starts from zero)
        soc, charge, discharge = _soc_trace(run.result)
        assert np.allclose(soc, np.concatenate([[0.0], soc[:-1]]) + 0.9 * charge - discharge, atol=1e-6)


def test_shift_semantics_are_positional_not_lexicographic():
    """Coords whose sorted order differs from declared order (string labels:
    lexicographic t0,t1,t10,... vs positional t0..t47). Both backends must
    couple the same neighbours."""
    n_s = 48
    labels = pd.Index([f't{i}' for i in range(n_s)], name='snapshot')
    assert list(labels.sort_values()) != list(labels)  # sorted != positional

    p_max = pd.Series({'wind': 80.0, 'gas': 70.0})
    t = np.arange(n_s)
    data = {
        'p_max': p_max,
        'cost': pd.Series({'wind': 1.0, 'gas': 40.0}),
        'load': pd.Series((110 + 60 * np.sin(2 * np.pi * t / 24)).round(3), index=labels),
    }
    coords = {'snapshot': labels, 'generator': pd.Index(p_max.index, name='generator')}

    original = STORAGE_YAML.read_text()
    assert 'dtype: int' in original
    with differential(original.replace('dtype: int', 'dtype: str'), data, coords):
        pass  # agreement on the objective is the whole assertion


RAMP_YAML = """
dimensions:
  snapshot: {dtype: int}
  generator: {values: [wind, gas]}
parameters:
  p_max: {dims: [generator]}
  cost: {dims: [generator]}
  load: {dims: [snapshot]}
  ramp_max: {dims: [generator]}
variables:
  p:
    foreach: [snapshot, generator]
    bounds: {lower: 0, upper: p_max}
constraints:
  balance:
    foreach: [snapshot]
    equations:
      - expression: sum(p, over=generator) == load
  ramp_up:
    foreach: [snapshot, generator]
    where: "snapshot > 0"
    equations:
      - expression: p - shift(p, snapshot=1) <= ramp_max
objectives:
  total_cost:
    sense: minimize
    equations:
      - expression: sum(p * cost, over=generator)
"""


def test_a_where_on_dimension_coordinates_means_the_same_on_both_lanes():
    """ROADMAP 5b: `where: "snapshot > 0"` must mean the same on both lanes.

    The README's ramp example uses exactly this — a time-coupling constraint
    that skips the first snapshot. It used to be eager-only: lowering refused
    dimension comparisons, so the same file built two different models.
    """
    n_s = 12
    rng = np.random.default_rng(11)
    data = {
        'p_max': pd.Series({'wind': 80.0, 'gas': 200.0}),
        'cost': pd.Series({'wind': 1.0, 'gas': 40.0}),
        'ramp_max': pd.Series({'wind': 100.0, 'gas': 25.0}),  # binding on gas
        'load': pd.Series(
            (rng.uniform(0.3, 0.9, n_s) * 200.0).round(3),
            index=pd.RangeIndex(n_s, name='snapshot'),
        ),
    }
    coords = {'snapshot': pd.RangeIndex(n_s, name='snapshot')}

    with differential(RAMP_YAML, data, coords) as run:
        # the mask must actually bite: the first snapshot is dropped per generator
        # (masked rows carry label -1 on the eager lane)
        active = int((run.model.constraints['ramp_up'].labels != -1).sum())
        assert active == (n_s - 1) * 2


# ---------------------------------------------------------------------------
# lowering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('expression', 'expected'),
    [
        ('roll(soc, snapshot=1)', Translate(Variable('soc'), 'snapshot', 1)),
        ('roll(soc, snapshot=-2)', Translate(Variable('soc'), 'snapshot', -2)),  # look-ahead
        ('shift(soc, snapshot=1)', Translate(Variable('soc'), 'snapshot', 1, wrap=False)),
    ],
)
def test_translation_lowers_to_a_bounded_halo(expression, expected):
    schema = schema_of(STORAGE_YAML)
    assert _lower_expr(resolved(expression, schema), schema, 't') == expected


@pytest.mark.parametrize(
    ('expression', 'match'),
    [
        ('roll(soc, nope=1)', r'roll\(nope=\.\.\.\) does not name a declared dimension'),
        ('roll(load, generator=1)', 'but the expression has dims'),
    ],
)
def test_translation_along_a_dim_the_expression_lacks_is_refused(expression, match):
    schema = schema_of(STORAGE_YAML)
    with pytest.raises(LanguageError, match=match):
        _lower_expr(resolved(expression, schema), schema, 't')
