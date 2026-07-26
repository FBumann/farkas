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

from linopy_yaml.errors import LanguageError
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
        'gen_bus': gens.set_index('generator')['bus'],
        'cap': lines.set_index('line')['cap'],
        'neg_cap': -lines.set_index('line')['cap'],
        'line_from': lines.set_index('line')['from_bus'],
        'line_to': lines.set_index('line')['to_bus'],
        'load': xr.DataArray.from_series(load.set_index(['snapshot', 'bus'])['value']),
    }
    coords = {
        'snapshot': pd.Index(sorted(load['snapshot'].unique()), name='snapshot'),
        'generator': pd.Index(gens['generator'], name='generator'),
        'bus': pd.Index(sorted(load['bus'].unique()), name='bus'),
        'line': pd.Index(lines['line'], name='line'),
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
    assert GroupSum(Variable('p'), mapping='gen_bus', into='bus') in _flatten(c.lhs)


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

    # an undeclared mapping or target dim is now caught in resolution, before
    # lowering ever sees the call — the name simply has no kind
    with pytest.raises(LanguageError, match="'nope' not found"):
        resolved('group_sum(p, nope, into=bus)', schema)
    with pytest.raises(LanguageError, match=r'into=nope\) does not name a declared dimension'):
        resolved('group_sum(p, gen_bus, into=nope)', schema)
    # shape errors stay at lowering: these names resolve, the arity does not fit
    with pytest.raises(LanguageError, match='exactly one dim'):
        _lower_expr(resolved('group_sum(p, load, into=bus)', schema), schema, 't')
    with pytest.raises(LanguageError, match='but the expression'):
        _lower_expr(resolved('group_sum(f, gen_bus, into=bus)', schema), schema, 't')
