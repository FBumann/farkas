"""Phase-3 gate: YAML lowers to the logical plan and matches the eager backend.

The dispatch example YAML runs through both backends with the same data:
eager `compat.build(...).solve()` is the oracle; the lowered Program
executes on DuckdbExecutor via solver_direct and the lp_file sink.
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
    DimensionComparison,
    Parameter,
    ParameterComparison,
    ParameterDefined,
    Sum,
    Variable,
)
from linopy_yaml.schema import MathSchema
from tests.conftest import resolved
from tests.oracle import compat

RTOL = 1e-9

DISPATCH_YAML = 'examples/dispatch.yaml'


@pytest.fixture
def dispatch_schema() -> MathSchema:
    with open(DISPATCH_YAML) as f:
        return MathSchema(**pyyaml.safe_load(f))


@pytest.fixture
def dispatch_inputs():
    rng = np.random.default_rng(3)
    n_s = 48
    p_max = pd.Series({'wind': 100.0, 'solar': 60.0, 'gas': 200.0})
    # distinct costs -> unique optimal vertex, so primals are comparable
    cost = pd.Series({'wind': 1.0, 'solar': 2.0, 'gas': 50.0})
    load = pd.Series(
        (rng.uniform(0.2, 0.8, n_s) * p_max.sum()).round(3),
        index=pd.RangeIndex(n_s, name='snapshot'),
    )
    data = {'p_max': p_max, 'load': load, 'cost': cost}
    coords = {'snapshot': pd.RangeIndex(n_s, name='snapshot')}
    return data, coords


def test_dispatch_yaml_differential(dispatch_schema, dispatch_inputs, tmp_path):
    data, coords = dispatch_inputs

    # oracle: the eager backend
    m = compat.build(DISPATCH_YAML, data=data, coords=coords)
    m.solve(solver_name='highs', output_flag=False)
    oracle = float(m.objective.value)
    assert np.isfinite(oracle)

    # relational: lower the same schema, execute with the same inputs
    program = lower_program(dispatch_schema)
    sources = tidy_sources(dispatch_schema, data, coords)

    with DuckdbExecutor(memory_limit='256MB') as ex:
        ex.build(program, sources)

        sol = ex.solve()
        assert sol.status == 'Optimal'
        assert sol.objective == pytest.approx(oracle, rel=RTOL)

        lp = tmp_path / 'dispatch.lp'
        ex.write_lp(lp)
        h = highspy.Highs()
        h.setOptionValue('output_flag', False)
        h.readModel(str(lp))
        h.run()
        assert h.getInfo().objective_function_value == pytest.approx(oracle, rel=RTOL)

        # primal agrees with the eager solution variable-by-variable
        eager_p = m.solution['p'].to_dataframe(name='value').reset_index()
        rel_p = sol.primal('p').to_pandas()
        merged = eager_p.merge(rel_p, on=['snapshot', 'generator'], suffixes=('_eager', '_rel'))
        # masked (gas is unmasked; all p_max > 0 here) rows align 1:1
        assert len(merged) == len(rel_p)
        assert np.allclose(merged['value_eager'], merged['value_rel'], atol=1e-6)


def test_lower_program_structure(dispatch_schema):
    program = lower_program(dispatch_schema)

    assert [p.name for p in program.parameters] == ['p_max', 'load', 'cost']
    (v,) = program.variables
    assert v.name == 'p'
    assert v.dims == ('snapshot', 'generator')
    assert v.where == ParameterComparison('p_max', '>', 0.0)
    assert v.upper == Parameter('p_max')

    (c,) = program.constraints
    assert c.name == 'power_balance'
    assert c.dims == ('snapshot',)
    assert c.lhs == Sum(Variable('p'), ('generator',))
    assert c.sense == '=='
    assert c.rhs == Parameter('load')

    assert program.objective.sense == 'min'
    # no sum: an objective totals every dim it carries, so writing one would
    # only restate what the objective already is
    assert program.objective.expression == Variable('p') * Parameter('cost')


def test_where_lowering(dispatch_schema):
    from linopy_yaml.lowering import _lower_where
    from linopy_yaml.resolution import Namespace

    ns = Namespace.of(dispatch_schema)
    assert _lower_where(None, ns, 't') is None
    assert _lower_where('True', ns, 't') is None  # True == no mask
    assert _lower_where('p_max', ns, 't') == ParameterDefined('p_max')
    pred = _lower_where('p_max > 0 AND NOT load == 0', ns, 't')
    assert pred is not None

    # dimension coordinates compare like parameters (ROADMAP 5b)
    assert _lower_where('snapshot > 5', ns, 't') == DimensionComparison('snapshot', '>', 5)


def test_unknown_where_name_is_an_error_in_both_lanes(dispatch_schema):
    """It used to be a scalar-False mask in the eager lane: a model that
    builds, solves, and is silently empty. Resolution makes it a load error."""
    from linopy_yaml.lowering import _lower_where
    from linopy_yaml.resolution import Namespace

    with pytest.raises(LanguageError, match="'no_such_param' not found"):
        _lower_where('no_such_param', Namespace.of(dispatch_schema), 't')


def test_sum_over_absent_dim_is_noop(dispatch_schema):
    # eager parity: sum(load, over=generator) leaves load unchanged
    from linopy_yaml.lowering import _lower_expr

    ast = resolved('sum(load, over=generator)', dispatch_schema)
    assert _lower_expr(ast, dispatch_schema, 't') == Parameter('load')


def test_unsupported_features_rejected(dispatch_schema):
    from linopy_yaml.lowering import _lower_expr

    # roll/shift are supported via plan.Translate, binary/integer via variable_type;
    # '**' and custom Python helpers remain outside the relational subset
    with pytest.raises(LanguageError, match=r"operator '\*\*'"):
        _lower_expr(resolved('p ** 2', dispatch_schema), dispatch_schema, 't')

    # binary is eligible now and lowers to vtype (see also test_router)
    schema_dict = pyyaml.safe_load(Path(DISPATCH_YAML).read_text())
    schema_dict['variables']['p']['binary'] = True
    schema_dict['variables']['p']['bounds'] = {}
    program = lower_program(MathSchema(**schema_dict))
    assert program.variable('p').variable_type == 'binary'
