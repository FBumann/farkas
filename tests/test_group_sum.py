"""group_sum: the same transport YAML through both backends.

Three-way differential on examples/transport.yaml:
  1. eager Model.from_yaml + solve (group_sum via linopy groupby)
  2. lowered Program -> DuckdbExecutor solver_direct
  3. hand-built indicator-matrix linopy model (independent oracle)
"""

from __future__ import annotations

from pathlib import Path

import highspy
import numpy as np
import pandas as pd
import pytest
import yaml as pyyaml

import farkas as fk
from farkas.errors import DataError, LanguageError
from farkas.lowering import lower_program
from farkas.relational import (
    DuckdbExecutor,
)
from farkas.relational.plan import (
    GroupSum,
    Variable,
)
from farkas.schema import MathSchema
from farkas.sources import tidy_sources
from tests.conftest import resolved
from tests.oracle import farkas_linopy, transport_eager_objective, xr

RTOL = 1e-9

TRANSPORT_YAML = 'examples/transport.yaml'


def _inputs(gens, lines, load):
    data = {
        'p_max': gens.set_index('generator')['p_max'],
        'cost': gens.set_index('generator')['cost'],
        'cap': lines.set_index('line')['cap'],
        'neg_cap': -lines.set_index('line')['cap'],
        'load': xr.DataArray.from_series(load.set_index(['snapshot', 'bus'])['value']),
    }
    # the two dims carrying declared coordinates arrive as frames: the label
    # column plus one column per coordinate
    coords = {
        'snapshot': pd.Index(sorted(load['snapshot'].unique()), name='snapshot'),
        'generator': gens[['generator', 'bus']],
        'bus': pd.Index(sorted(load['bus'].unique()), name='bus'),
        'line': lines[['line', 'from_bus', 'to_bus']].rename(columns={'from_bus': 'from', 'to_bus': 'to'}),
    }
    return data, coords


def test_transport_yaml_differential(transport_data, tmp_path):
    gens, lines, load = transport_data
    data, coords = _inputs(gens, lines, load)

    # independent oracle (indicator matrices, no group_sum involved)
    oracle = transport_eager_objective(gens, lines, load)
    assert np.isfinite(oracle)

    # eager backend through the YAML group_sum helper
    m = farkas_linopy.build(TRANSPORT_YAML, data=data, coords=coords)
    m.solve(solver_name='highs', output_flag=False)
    assert float(m.objective.value) == pytest.approx(oracle, rel=RTOL)

    # relational backend through lowering
    schema = MathSchema(**pyyaml.safe_load(Path(TRANSPORT_YAML).read_text()))
    with DuckdbExecutor(memory_limit='256MB') as ex:
        ex.build(lower_program(schema), tidy_sources(schema, data, coords))
        sol = ex.solve()
        assert sol.status == 'Optimal'
        assert sol.objective == pytest.approx(oracle, rel=RTOL)

        lp = tmp_path / 'transport.lp'
        ex.write_lp(lp)
        h = highspy.Highs()
        h.setOptionValue('output_flag', False)
        h.readModel(str(lp))
        h.run()
        assert h.getInfo().objective_function_value == pytest.approx(oracle, rel=RTOL)


def test_group_sum_lowering_structure():
    schema = MathSchema(**pyyaml.safe_load(Path(TRANSPORT_YAML).read_text()))
    program = lower_program(schema)

    (c,) = program.constraints
    assert c.dims == ('snapshot', 'bus')
    # lhs contains the three GroupSum pieces
    assert GroupSum(Variable('p'), over='generator', coordinate='bus', into='bus') in _flatten(c.lhs)
    assert GroupSum(Variable('f'), over='line', coordinate='to', into='bus') in _flatten(c.lhs)


def _flatten(expr):
    from farkas.relational.plan import (
        Add,
        Negate,
    )

    if isinstance(expr, Add):
        return _flatten(expr.left) + _flatten(expr.right)
    if isinstance(expr, Negate):
        return _flatten(expr.operand)
    return [expr]


def test_group_sum_lowering_errors():
    schema = MathSchema(**pyyaml.safe_load(Path(TRANSPORT_YAML).read_text()))
    from farkas.lowering import _lower_expr

    # an undeclared dim, or a coordinate the dim does not declare, is caught in
    # resolution before lowering ever sees the call
    with pytest.raises(LanguageError, match=r'over=nope\) does not name a declared dimension'):
        resolved('group_sum(p, over=nope, by=bus)', schema)
    with pytest.raises(LanguageError, match=r"by=nope\) does not name a coordinate of 'generator'"):
        resolved('group_sum(p, over=generator, by=nope)', schema)
    # a coordinate declared on a *different* dim is not in scope either
    with pytest.raises(LanguageError, match=r"by=to\) does not name a coordinate of 'generator'"):
        resolved('group_sum(p, over=generator, by=to)', schema)
    # the names resolve and the arity fits, so what is left is a dim rule:
    # lowering raises it by asking `dimensions`, not by restating it
    with pytest.raises(LanguageError, match='but the expression'):
        _lower_expr(resolved('group_sum(f, over=generator, by=bus)', schema), schema, 't')


# ---------------------------------------------------------------------------
# the hole a coordinate closes: a label that is not a coordinate of its target
# ---------------------------------------------------------------------------


def _mistyped(gens, lines, load):
    """Point one generator at a bus that does not exist."""
    bad = gens.copy()
    bad.loc[bad.index[0], 'bus'] = 'nowhere'
    return _inputs(bad, lines, load)


def test_mistyped_coordinate_is_refused_relationally(transport_data):
    """Before coordinates were declared this built and solved: the mapping's
    value column was promoted to an index unchecked, and the inner join that
    places the terms dropped the generator out of its balance silently."""
    gens, lines, load = transport_data
    data, coords = _mistyped(gens, lines, load)
    schema = MathSchema(**pyyaml.safe_load(Path(TRANSPORT_YAML).read_text()))
    with DuckdbExecutor(memory_limit='256MB') as ex, pytest.raises(DataError, match="not 'bus' coordinates"):
        ex.build(lower_program(schema), tidy_sources(schema, data, coords))


def test_mistyped_coordinate_is_refused_eagerly(transport_data):
    gens, lines, load = transport_data
    data, coords = _mistyped(gens, lines, load)
    with pytest.raises(DataError, match="not 'bus' coordinates"):
        farkas_linopy.build(TRANSPORT_YAML, data=data, coords=coords)


def test_a_coordinate_must_be_single_valued(transport_data):
    """Two rows disagreeing about a generator's bus is a data bug, not a
    silently-picked winner."""
    gens, lines, load = transport_data
    other = 's' if gens['bus'].iloc[0] != 's' else 'n'
    doubled = pd.concat([gens, gens.head(1).assign(bus=other)])
    data, coords = _inputs(doubled, lines, load)
    schema = MathSchema(**pyyaml.safe_load(Path(TRANSPORT_YAML).read_text()))
    with DuckdbExecutor(memory_limit='256MB') as ex, pytest.raises(DataError, match='more than one value'):
        ex.build(lower_program(schema), tidy_sources(schema, data, coords))


def test_a_coordinate_bearing_dim_needs_an_index_source(transport_data):
    """A coordinate cannot be inferred from the parameters that use the dim —
    inferring it is what would let a typo extend the label space."""
    gens, lines, load = transport_data
    data, coords = _inputs(gens, lines, load)
    del coords['generator']
    schema = MathSchema(**pyyaml.safe_load(Path(TRANSPORT_YAML).read_text()))
    with DuckdbExecutor(memory_limit='256MB') as ex, pytest.raises(DataError, match='no index source'):
        ex.build(lower_program(schema), tidy_sources(schema, data, coords))


PARTIAL_YAML = """
dimensions:
  g: {dtype: str}
  item:
    dtype: str
    coords: {grp: g}
parameters:
  cap: {dims: [item]}
  target: {dims: [g]}
variables:
  x:
    foreach: [item]
    bounds: {lower: 0, upper: cap}
constraints:
  meet:
    foreach: [g]
    equations:
      - expression: group_sum(x, over=item, by=grp) >= target
objectives:
  obj:
    sense: minimize
    equations:
      - expression: sum(x, over=item)
"""


def _partial_inputs(grp_labels):
    """`item` carries coordinate `grp`; *grp_labels* is one label per item."""
    items = ['i0', 'i1', 'i2']
    index = pd.DataFrame({'item': items, 'grp': grp_labels})
    return (
        {  # relational sources
            'item': index,
            'g': pd.DataFrame({'g': ['g0']}),
            'cap': pd.DataFrame({'item': items, 'value': [5.0, 5.0, 5.0]}),
            'target': pd.DataFrame({'g': ['g0'], 'value': [3.0]}),
        },
        {  # eager data / coords
            'cap': pd.Series([5.0, 5.0, 5.0], index=pd.Index(items, name='item')),
            'target': pd.Series([3.0], index=pd.Index(['g0'], name='g')),
        },
        {'item': index, 'g': pd.Index(['g0'], name='g')},
    )


def test_a_partial_coordinate_places_its_orphans_nowhere(tmp_path):
    """A null coordinate means "this label is in no group", not "typo".

    Row absence is the language's idiom for "not present" everywhere else —
    an absent parameter row is a structural zero — and a coordinate is the one
    place it used to be an error. `i2` belongs to no group, so `group_sum`
    places its terms nowhere and only `i0`/`i1` can meet the target of 3.
    """
    path = tmp_path / 'partial.yaml'
    path.write_text(PARTIAL_YAML)
    sources, data, coords = _partial_inputs(['g0', 'g0', None])

    with fk.solve(path, sources) as sol:
        assert sol.status == 'Optimal'
        assert sol.objective == pytest.approx(3.0)
        # the orphan is still a variable; it just carries no group obligation
        assert sol.primal('x').set_index('item')['value']['i2'] == pytest.approx(0.0)

    model = farkas_linopy.build(path, data=data, coords=coords)
    model.solve(solver_name='highs', output_flag=False)
    assert float(model.objective.value) == pytest.approx(3.0)
