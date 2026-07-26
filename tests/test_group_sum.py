"""group_sum: the transport YAML through both backends, and what coordinates buy.

Three-way differential on examples/transport.yaml:
  1. eager farkas_linopy.build + solve (group_sum via linopy groupby)
  2. lowered Program -> DuckdbExecutor solver_direct, plus the LP file
  3. hand-built indicator-matrix linopy model (an independent oracle that
     involves no group_sum at all)
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import farkas as fk
from farkas.errors import DataError, LanguageError
from farkas.lowering import _lower_expr, lower_program
from farkas.relational import DuckdbExecutor
from farkas.relational.plan import (
    Add,
    GroupSum,
    Negate,
    Variable,
)
from farkas.sources import tidy_sources
from tests.conftest import resolved, schema_of
from tests.differential import RTOL, differential
from tests.oracle import farkas_linopy, transport_eager_objective, xr

TRANSPORT_YAML = Path('examples/transport.yaml')


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


def test_transport_yaml_agrees_with_an_independent_oracle(transport_data):
    gens, lines, load = transport_data
    data, coords = _inputs(gens, lines, load)

    # indicator matrices, no group_sum involved — an oracle for the oracle
    independent = transport_eager_objective(gens, lines, load)
    assert np.isfinite(independent)

    with differential(TRANSPORT_YAML, data, coords, lp=True) as run:
        assert run.oracle == pytest.approx(independent, rel=RTOL)


# ---------------------------------------------------------------------------
# lowering
# ---------------------------------------------------------------------------


def _flatten(expr):
    if isinstance(expr, Add):
        return _flatten(expr.left) + _flatten(expr.right)
    if isinstance(expr, Negate):
        return _flatten(expr.operand)
    return [expr]


def test_group_sum_lowers_to_one_node_per_injection_term():
    program = lower_program(schema_of(TRANSPORT_YAML))

    (c,) = program.constraints
    assert c.dims == ('snapshot', 'bus')
    terms = _flatten(c.lhs)
    assert GroupSum(Variable('p'), over='generator', coordinate='bus', into='bus') in terms
    assert GroupSum(Variable('f'), over='line', coordinate='to', into='bus') in terms


@pytest.mark.parametrize(
    ('expression', 'match'),
    [
        # an undeclared dim, or a coordinate the dim does not declare, is caught
        # in resolution before lowering ever sees the call
        ('group_sum(p, over=nope, by=bus)', r'over=nope\) does not name a declared dimension'),
        ('group_sum(p, over=generator, by=nope)', r"by=nope\) does not name a coordinate of 'generator'"),
        # a coordinate declared on a *different* dim is not in scope either
        ('group_sum(p, over=generator, by=to)', r"by=to\) does not name a coordinate of 'generator'"),
    ],
)
def test_a_name_group_sum_cannot_resolve_is_refused(expression, match):
    with pytest.raises(LanguageError, match=match):
        resolved(expression, schema_of(TRANSPORT_YAML))


def test_grouping_an_expression_that_lacks_the_dim_is_refused():
    """The names resolve and the arity fits, so what is left is a dim rule:
    lowering raises it by asking `dimensions`, not by restating it."""
    schema = schema_of(TRANSPORT_YAML)
    with pytest.raises(LanguageError, match='but the expression'):
        _lower_expr(resolved('group_sum(f, over=generator, by=bus)', schema), schema, 't')


# ---------------------------------------------------------------------------
# the hole a coordinate closes: a label that is not a coordinate of its target
# ---------------------------------------------------------------------------


def _relationally(data, coords):
    schema = schema_of(TRANSPORT_YAML)
    with DuckdbExecutor(memory_limit='256MB') as ex:
        ex.build(lower_program(schema), tidy_sources(schema, data, coords))


def test_a_mistyped_coordinate_is_refused_on_both_lanes(transport_data):
    """Before coordinates were declared this built and solved: the mapping's
    value column was promoted to an index unchecked, and the inner join that
    places the terms dropped the generator out of its balance silently."""
    gens, lines, load = transport_data
    bad = gens.copy()
    bad.loc[bad.index[0], 'bus'] = 'nowhere'  # a bus that does not exist
    data, coords = _inputs(bad, lines, load)

    with pytest.raises(DataError, match="not 'bus' coordinates"):
        _relationally(data, coords)
    with pytest.raises(DataError, match="not 'bus' coordinates"):
        farkas_linopy.build(TRANSPORT_YAML, data=data, coords=coords)


def test_a_coordinate_must_be_single_valued(transport_data):
    """Two rows disagreeing about a generator's bus is a data bug, not a
    silently-picked winner."""
    gens, lines, load = transport_data
    other = 's' if gens['bus'].iloc[0] != 's' else 'n'
    doubled = pd.concat([gens, gens.head(1).assign(bus=other)])

    with pytest.raises(DataError, match='more than one value'):
        _relationally(*_inputs(doubled, lines, load))


def test_a_coordinate_bearing_dim_needs_an_index_source(transport_data):
    """A coordinate cannot be inferred from the parameters that use the dim —
    inferring it is what would let a typo extend the label space."""
    gens, lines, load = transport_data
    data, coords = _inputs(gens, lines, load)
    del coords['generator']

    with pytest.raises(DataError, match='no index source'):
        _relationally(data, coords)


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
