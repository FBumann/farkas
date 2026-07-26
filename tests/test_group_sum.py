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

from linopy_yaml.errors import DataError, LanguageError
from linopy_yaml.lowering import lower_program, tidy_sources
from linopy_yaml.relational import (
    DuckdbExecutor,
)
from linopy_yaml.relational.plan import (
    GroupSum,
    Variable,
)
from linopy_yaml.schema import MathSchema
from tests.conftest import resolved
from tests.oracle import compat, transport_eager_objective, xr

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
    m = compat.build(TRANSPORT_YAML, data=data, coords=coords)
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
    from linopy_yaml.relational.plan import (
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
    from linopy_yaml.lowering import _lower_expr

    # an undeclared dim, or a coordinate the dim does not declare, is caught in
    # resolution before lowering ever sees the call
    with pytest.raises(LanguageError, match=r'over=nope\) does not name a declared dimension'):
        resolved('group_sum(p, over=nope, by=bus)', schema)
    with pytest.raises(LanguageError, match=r"by=nope\) does not name a coordinate of 'generator'"):
        resolved('group_sum(p, over=generator, by=nope)', schema)
    # a coordinate declared on a *different* dim is not in scope either
    with pytest.raises(LanguageError, match=r"by=to\) does not name a coordinate of 'generator'"):
        resolved('group_sum(p, over=generator, by=to)', schema)
    # shape errors stay at lowering: the names resolve, the operand lacks the dim
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
        compat.build(TRANSPORT_YAML, data=data, coords=coords)


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
