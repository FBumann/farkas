"""The scoping divergences, checked against the oracle lane itself.

`test_resolution.py` checks the language rules. This module checks the thing
that actually mattered: that the *eager* lane now refuses what the relational
lane refuses, in the same place, for the same reason. Before resolution was a
pass, each of these built a model on one lane and raised on the other.
"""

from __future__ import annotations

import pytest
import yaml as pyyaml

import linopy_yaml as ly
from tests.oracle import compat  # skips the module without the [compat] extra

MODEL = {
    'dimensions': {'snapshot': {'dtype': 'int'}, 'generator': {'values': ['wind', 'gas']}},
    'parameters': {
        'p_max': {'dims': ['generator']},
        'cost': {'dims': ['generator']},
        'load': {'dims': ['snapshot']},
    },
    'variables': {'p': {'foreach': ['snapshot', 'generator'], 'bounds': {'lower': 0, 'upper': 'p_max'}}},
    'constraints': {
        'balance': {'foreach': ['snapshot'], 'equations': [{'expression': 'sum(p, over=generator) == load'}]}
    },
    'objectives': {'total': {'sense': 'minimize', 'equations': [{'expression': 'sum(p * cost, over=generator)'}]}},
}


@pytest.fixture
def data():
    import pandas as pd

    return {
        'p_max': pd.Series({'wind': 100.0, 'gas': 200.0}),
        'cost': pd.Series({'wind': 0.0, 'gas': 50.0}),
        'load': pd.Series([80.0] * 4, index=pd.RangeIndex(4, name='snapshot')),
    }


@pytest.fixture
def coords():
    import pandas as pd

    return {'snapshot': pd.RangeIndex(4, name='snapshot')}


def _write(tmp_path, **patch):
    import copy

    raw = copy.deepcopy(MODEL)
    for dotted, value in patch.items():
        node = raw
        *path, leaf = dotted.split('.')
        for key in path:
            node = node[key]
        node[leaf] = value
    path = tmp_path / 'm.yaml'
    path.write_text(pyyaml.safe_dump(raw))
    return path


@pytest.mark.parametrize(
    ('where', 'match', 'was'),
    [
        ('typo_name > 0', "'typo_name' not found", 'eager built 0 live variables; relational raised'),
        ('p_max > cost', 'compares two parameters', "eager compared parameters; relational compared to 'cost'"),
        ('nonexistent', "'nonexistent' not found", 'eager masked everything out; relational raised'),
    ],
)
def test_both_lanes_refuse_the_same_where(tmp_path, data, coords, where, match, was):
    path = _write(tmp_path, **{'variables.p.where': where})

    with pytest.raises(ValueError, match=match):
        compat.build(path, data=data, coords=coords)  # was: {was}

    with pytest.raises(ValueError, match=match):
        ly.check(path)


def test_a_valid_model_still_builds_on_both_lanes(tmp_path, data, coords):
    """The guard rails do not fence off the language: the same file still
    builds where it did before."""
    path = _write(tmp_path, **{'variables.p.where': 'p_max > 0'})

    m = compat.build(path, data=data, coords=coords)
    assert m.variables['p'].size == 8

    with ly.build(path, data, coords=coords) as ex:
        sol = ex.solve()
        assert sol.status == 'Optimal'
