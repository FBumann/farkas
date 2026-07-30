"""Named sub-expressions and macros — YAML-defined, schema-local.

Both expand to core AST before backend dispatch, so one differential test at
the end proves the whole feature works identically on both backends.
"""

from __future__ import annotations

import numpy as np
import pytest

from lpspec.expansion import parse_and_expand
from lpspec.expression_parser import parse_expression
from lpspec.schema import MathSchema
from lpspec.validation import validate_expressions
from tests.differential import differential
from tests.oracle import pd

WEIGHTED_SUM = {
    'args': ['array', 'weights'],
    'kwargs': ['over'],
    'template': 'sum(array * weights, over=over)',
}


def make_schema(
    expressions: dict[str, str] | None = None,
    macros: dict | None = None,
    **overrides,
) -> MathSchema:
    base = {
        'dimensions': {
            'snapshot': {'dtype': 'int'},
            'generator': {'values': ['wind', 'gas']},
        },
        'parameters': {
            'p_max': {'dims': ['generator']},
            'cost': {'dims': ['generator']},
            'load': {'dims': ['snapshot']},
        },
        'variables': {
            'p': {
                'foreach': ['snapshot', 'generator'],
                'bounds': {'lower': 0, 'upper': 'p_max'},
            }
        },
        'constraints': {
            'balance': {
                'foreach': ['snapshot'],
                'expression': 'sum(p, over=generator) == load',
            }
        },
        'objectives': {
            'total': {
                'sense': 'minimize',
                'expression': 'sum(p * cost, over=generator)',
            }
        },
    }
    if expressions is not None:
        base['expressions'] = expressions
    if macros is not None:
        base['macros'] = macros
    base.update(overrides)
    return MathSchema(**base)


# ---------------------------------------------------------------------------
# named sub-expressions
# ---------------------------------------------------------------------------


def test_named_expression_splices():
    schema = make_schema({'gen_cost': 'p * cost'})
    got = parse_and_expand('sum(gen_cost, over=generator)', schema)
    want = parse_expression('sum(p * cost, over=generator)')
    assert got == want


def test_named_expressions_nest():
    schema = make_schema({'gen_cost': 'p * cost', 'total_cost': 'sum(gen_cost, over=generator)'})
    got = parse_and_expand('total_cost + 1', schema)
    want = parse_expression('sum(p * cost, over=generator) + 1')
    assert got == want


def test_named_expression_cycle_raises():
    schema = make_schema({'a': 'b + 1', 'b': 'a + 1'})
    with pytest.raises(ValueError, match='circular expression reference: a -> b -> a'):
        parse_and_expand('a', schema)


def test_named_expression_no_comparison():
    schema = make_schema({'bad': 'p == load'})
    with pytest.raises(ValueError, match='must not contain a comparison'):
        parse_and_expand('bad + 1', schema)


def test_name_collision_rejected_at_schema_level():
    with pytest.raises(ValueError, match='collides with the parameter of the same name'):
        make_schema({'load': 'p * cost'})


def test_expand_handles_comparison_at_top():
    schema = make_schema({'total_gen': 'sum(p, over=generator)'})
    got = parse_and_expand('total_gen == load', schema)
    want = parse_expression('sum(p, over=generator) == load')
    assert got == want


def test_validation_reports_bad_named_expression():
    schema = make_schema({'broken': 'sum(nope, over=generator)'})
    with pytest.raises(ValueError, match="Named expression 'broken'"):
        validate_expressions(schema)


# ---------------------------------------------------------------------------
# macros
# ---------------------------------------------------------------------------


def test_macro_expansion():
    schema = make_schema(macros={'weighted_sum': WEIGHTED_SUM})
    got = parse_and_expand('weighted_sum(p, cost, over=generator)', schema)
    want = parse_expression('sum(p * cost, over=generator)')
    assert got == want


def test_macro_formals_shadow_model_names():
    # formal 'load' shadows the model parameter 'load' inside the body
    schema = make_schema(macros={'double': {'args': ['load'], 'template': 'load + load'}})
    got = parse_and_expand('double(p)', schema)
    want = parse_expression('p + p')
    assert got == want


def test_macro_args_may_use_named_expressions():
    schema = make_schema(
        {'gen_cost': 'p * cost'},
        macros={'twice': {'args': ['x'], 'template': 'x + x'}},
    )
    got = parse_and_expand('twice(gen_cost)', schema)
    want = parse_expression('(p * cost) + (p * cost)')
    assert got == want


def test_macro_body_may_use_macros_and_named_expressions():
    schema = make_schema(
        macros={
            'total': {'args': ['x'], 'template': 'sum(x, over=generator)'},
            'total_cost': {'template': 'total(p * cost)'},
        }
    )
    got = parse_and_expand('total_cost()', schema)
    want = parse_expression('sum(p * cost, over=generator)')
    assert got == want


def test_macro_arity_errors():
    schema = make_schema(macros={'ws': WEIGHTED_SUM})
    with pytest.raises(ValueError, match='expects 2 positional'):
        parse_and_expand('ws(p, over=generator)', schema)
    with pytest.raises(ValueError, match='keyword argument'):
        parse_and_expand('ws(p, cost)', schema)


def test_macro_cycle_raises():
    schema = make_schema(
        macros={
            'loop_a': {'template': 'loop_b() + 1'},
            'loop_b': {'template': 'loop_a() + 1'},
        }
    )
    with pytest.raises(ValueError, match='circular macro reference'):
        parse_and_expand('loop_a()', schema)


def test_macro_collisions_rejected():
    with pytest.raises(ValueError, match='collides with the parameter of the same name'):
        make_schema(macros={'load': {'args': ['a'], 'template': 'a'}})
    with pytest.raises(ValueError, match='collides with the named expression'):
        make_schema({'thing': 'p * cost'}, macros={'thing': {'args': ['a'], 'template': 'a'}})
    # helper names are reserved for every kind of declaration, not just macros,
    # and the collision is caught building the schema rather than validating it
    with pytest.raises(ValueError, match="collides with the built-in helper 'sum'"):
        make_schema(macros={'sum': {'args': ['a'], 'template': 'a'}})
    with pytest.raises(ValueError, match="collides with the built-in helper 'sum'"):
        MathSchema(dimensions={'sum': {'values': [1]}})


def test_duplicate_formals_rejected():
    with pytest.raises(ValueError, match='duplicate formal'):
        make_schema(macros={'m': {'args': ['a'], 'kwargs': ['a'], 'template': 'a'}})


def test_macro_templates_validated_even_when_unused():
    # schema-local macros make load-time validation complete: a typo in a
    # template the model never calls is still caught
    schema = make_schema(macros={'unused': {'args': ['x'], 'template': 'x * cots'}})
    with pytest.raises(ValueError, match=r"Macro 'unused'.*'cots' not found"):
        validate_expressions(schema)

    schema = make_schema(macros={'bad': {'args': ['a', 'b'], 'template': 'a == b'}})
    with pytest.raises(ValueError, match='must not contain a comparison'):
        validate_expressions(schema)


# ---------------------------------------------------------------------------
# end to end: both backends, same self-contained YAML, same answer
# ---------------------------------------------------------------------------


EXPANSION_YAML = """
dimensions:
  snapshot: {dtype: int}
  generator: {values: [wind, solar, gas]}
parameters:
  p_max: {dims: [generator]}
  cost: {dims: [generator]}
  load: {dims: [snapshot]}
expressions:
  total_generation: sum(p, over=generator)
macros:
  weighted_sum:
    args: [array, weights]
    kwargs: [over]
    template: sum(array * weights, over=over)
variables:
  p:
    foreach: [snapshot, generator]
    where: "p_max > 0"
    bounds: {lower: 0, upper: p_max}
constraints:
  balance:
    foreach: [snapshot]
    expression: total_generation == load
objectives:
  total_cost:
    sense: minimize
    expression: weighted_sum(p, cost, over=generator)
"""


def test_a_macro_and_a_named_expression_mean_the_same_on_both_lanes():
    """One end-to-end test carries the whole feature: both constructs expand to
    core AST before dispatch, so if the lanes agree here they agree at all."""
    rng = np.random.default_rng(5)
    n_s = 24
    data = {
        'p_max': pd.Series({'wind': 100.0, 'solar': 60.0, 'gas': 200.0}),
        'cost': pd.Series({'wind': 1.0, 'solar': 2.0, 'gas': 50.0}),
        'load': pd.Series(
            (rng.uniform(0.2, 0.8, n_s) * 360.0).round(3),
            index=pd.RangeIndex(n_s, name='snapshot'),
        ),
    }
    coords = {'snapshot': pd.RangeIndex(n_s, name='snapshot')}

    with differential(EXPANSION_YAML, data, coords):
        pass  # agreement on the objective is the whole assertion


def test_unknown_helper_rejected_at_load_time_with_the_rewrite():
    """An unknown helper fails validation, before either backend is chosen."""
    schema = make_schema(
        constraints={
            'c': {
                'foreach': ['snapshot'],
                'expression': 'my_python_helper(p) <= load',
            }
        }
    )
    with pytest.raises(ValueError) as exc:
        validate_expressions(schema)

    message = str(exc.value)
    assert 'my_python_helper' in message
    # the rejection teaches the rewrite instead of pointing at another backend
    assert 'macros:' in message
    assert 'escape' in message
    assert 'eager' not in message.lower()
