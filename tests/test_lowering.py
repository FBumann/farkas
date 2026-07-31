"""Phase-3 gate: YAML lowers to the logical plan and matches the eager backend.

The dispatch example runs through both backends with the same data, and the
lowered ``Program`` is read back node by node — the plan is the contract
between the language and the engine, so its shape is asserted directly rather
than only through the answer it produces.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from lpspec.errors import DataError, DimensionError, LanguageError
from lpspec.language.resolution import Namespace
from lpspec.language.schema import MathSchema
from lpspec.lowering import _lower_expr, _lower_where, lower_program
from lpspec.relational.plan import (
    DimensionComparison,
    Parameter,
    ParameterComparison,
    ParameterDefined,
    Sum,
    Variable,
)
from lpspec.sources import tidy_sources
from tests.conftest import resolved, schema_of
from tests.differential import differential

DISPATCH_YAML = Path('examples/dispatch.yaml')


@pytest.fixture
def dispatch_schema() -> MathSchema:
    return schema_of(DISPATCH_YAML)


def test_dispatch_yaml_agrees_variable_by_variable(dispatch_yaml, dispatch_inputs):
    data, coords = dispatch_inputs

    with differential(DISPATCH_YAML, data, coords, lp=True) as run:
        # primal agrees with the eager solution variable-by-variable, not only
        # in total — an objective can agree while the dispatch behind it differs
        eager_p = run.model.solution['p'].to_dataframe(name='value').reset_index()
        rel_p = run.result.to_pandas('p')
        merged = eager_p.merge(rel_p, on=['snapshot', 'generator'], suffixes=('_eager', '_rel'))
        # masked (gas is unmasked; all p_max > 0 here) rows align 1:1
        assert len(merged) == len(rel_p)
        assert np.allclose(merged['value_eager'], merged['value_rel'], atol=1e-6)


# ---------------------------------------------------------------------------
# the plan the language lowers to
# ---------------------------------------------------------------------------


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


@pytest.mark.parametrize(
    ('where', 'expected'),
    [
        (None, None),
        ('True', None),  # True == no mask
        ('p_max', ParameterDefined('p_max')),
        # dimension coordinates compare like parameters (ROADMAP 5b)
        ('snapshot > 5', DimensionComparison('snapshot', '>', 5)),
    ],
)
def test_where_lowering(dispatch_schema, where, expected):
    assert _lower_where(where, Namespace.of(dispatch_schema), 't') == expected


def test_a_compound_where_lowers_to_something(dispatch_schema):
    assert _lower_where('p_max > 0 AND NOT load == 0', Namespace.of(dispatch_schema), 't') is not None


def test_an_unknown_where_name_is_an_error_at_lowering_too(dispatch_schema):
    """It used to be a scalar-False mask in the eager lane: a model that
    builds, solves, and is silently empty. Resolution makes it a load error."""
    with pytest.raises(LanguageError, match="'no_such_param' not found"):
        _lower_where('no_such_param', Namespace.of(dispatch_schema), 't')


def test_sum_over_absent_dim_raises_at_lowering_too(dispatch_schema):
    """A no-op sum is an error at *every* layer, not only at the front door.

    SPEC §"dims" and alpha.4 settled the language question: summing over a dim
    the operand does not carry builds a model that solves and is wrong, so it
    is an error rather than the silent identity it once was. ``check_schema``
    raises it for anything entering through ``lps.check``; this pins that
    ``_lower_expr`` does not quietly disagree one layer down, which is what it
    used to do — it returned the operand unchanged, and the comment claiming
    eager parity outlived the parity.
    """
    with pytest.raises(DimensionError, match='no-op that builds and solves wrong'):
        _lower_expr(resolved('sum(load, over=generator)', dispatch_schema), dispatch_schema, 't')


def test_the_power_operator_stays_outside_the_relational_subset(dispatch_schema):
    """roll/shift lower to plan.Translate and binary/integer to variable_type;
    '**' has no affine reading at all, so it has nowhere to go."""
    with pytest.raises(LanguageError, match=r"operator '\*\*'"):
        _lower_expr(resolved('p ** 2', dispatch_schema), dispatch_schema, 't')


def test_a_binary_variable_lowers_to_a_vtype():
    program = lower_program(schema_of(DISPATCH_YAML, **{'variables.p.binary': True, 'variables.p.bounds': {}}))
    assert program.variable('p').variable_type == 'binary'


# ---------------------------------------------------------------------------
# binding an index to a dim: by name where there is one, by position otherwise
# ---------------------------------------------------------------------------

NETWORK = {
    'dimensions': {'from_bus': {'values': ['n1', 'n2']}, 'to_bus': {'values': ['n1', 'n2']}},
    'parameters': {'cap': {'dims': ['from_bus', 'to_bus']}},
    'variables': {'f': {'foreach': ['from_bus', 'to_bus'], 'bounds': {'lower': 0, 'upper': 'cap'}}},
    'objectives': {'c': {'sense': 'maximize', 'expression': 'f'}},
}

#: Asymmetric, so a transposition changes the answer rather than hiding in it.
CAPS = {('n1', 'n1'): 1.0, ('n2', 'n1'): 5.0, ('n1', 'n2'): 500.0, ('n2', 'n2'): 1.0}


def _tidy_cap(names):
    import pandas as pd

    index = pd.MultiIndex.from_tuples(list(CAPS), names=names)
    # tidy_sources normalises to a frame, so read the columns back by name —
    # which is the point: a transposition would show up as swapped values
    frame = tidy_sources(MathSchema(**NETWORK), {'cap': pd.Series(list(CAPS.values()), index=index)})['cap'].collect()
    table = frame.to_dict(as_series=False)
    return dict(zip(zip(table['from_bus'], table['to_bus'], strict=True), table['value'], strict=True))


def test_a_named_index_binds_by_name_not_position():
    """Two dims over the same label space make a transposed index type-check
    and cover every coordinate, so nothing downstream can catch it. Was: the
    declared dims overwrote the user's level names and the matrix came out
    transposed, with no error.
    """
    assert _tidy_cap(['from_bus', 'to_bus']) == CAPS
    assert _tidy_cap(['to_bus', 'from_bus']) == {(f, t): v for (t, f), v in CAPS.items()}


def test_an_unnamed_index_still_binds_positionally():
    assert _tidy_cap([None, None]) == CAPS


def test_an_index_name_outside_the_declared_dims_is_an_error():
    with pytest.raises(DataError, match='do not match its declared dims'):
        _tidy_cap(['banana', 'to_bus'])
