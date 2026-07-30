"""Pydantic-level validation: what a well-formed declaration looks like.

Everything here is decided from the YAML mapping alone — no expressions
parsed, no dims inferred, no data. The rules are mostly of two kinds
(a name that must be declared, a key that must be spelled right), so they are
stated as tables: a new rule is a row, and a rule that silently stops firing
is a row that stops failing.
"""

import pytest
from pydantic import ValidationError

from lpspec.schema import MathSchema


def test_empty_schema():
    s = MathSchema.model_validate({})
    assert s.dimensions == {}
    assert s.variables == {}


def test_minimal_schema():
    s = MathSchema.model_validate(
        {
            'dimensions': {'x': {'values': [1, 2, 3]}},
            'parameters': {'a': {'dims': ['x']}},
            'variables': {'v': {'foreach': ['x']}},
        }
    )
    assert 'x' in s.dimensions
    assert s.parameters['a'].dims == ['x']
    assert s.variables['v'].foreach == ['x']


# ---------------------------------------------------------------------------
# a name a declaration uses must be declared
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('section', 'body', 'match'),
    [
        pytest.param('parameters', {'a': {'dims': ['y']}}, "undeclared dimension 'y'", id='parameter-dim'),
        pytest.param('variables', {'v': {'foreach': ['y']}}, "undeclared dimension 'y'", id='variable-foreach'),
        pytest.param(
            'constraints',
            {'c': {'foreach': ['y'], 'expression': 'v == 0'}},
            "undeclared dimension 'y'",
            id='constraint-foreach',
        ),
        pytest.param(
            'variables',
            {'v': {'foreach': ['x'], 'bounds': {'upper': 'nonexistent'}}},
            "'nonexistent' is not a declared parameter",
            id='bound-parameter',
        ),
    ],
)
def test_an_undeclared_name_is_rejected(section, body, match):
    with pytest.raises(ValidationError, match=match):
        MathSchema.model_validate({'dimensions': {'x': {'values': [1]}}, section: body})


def test_an_omitted_bound_means_unbounded_all_the_way_down():
    """A declaration that omits a bound means unbounded, exactly as in
    ``linopy.Model.add_variables`` — never an implicit ``>= 0``.

    Nothing else pins this: both lanes read the same default, so the
    differential tests agree with each other whatever it is. The second half
    checks the relational lane carries the default through rather than
    re-defaulting it on the way to the plan.
    """
    from lpspec.lowering import _bound_expression

    s = MathSchema.model_validate({'dimensions': {'x': {'values': [1]}}, 'variables': {'v': {'foreach': ['x']}}})
    bounds = s.variables['v'].bounds

    assert (bounds.lower, bounds.upper) == (float('-inf'), float('inf'))
    assert _bound_expression(bounds.lower).value == float('-inf')
    assert _bound_expression(bounds.upper).value == float('inf')


def test_a_declared_bound_parameter_is_accepted():
    s = MathSchema.model_validate(
        {
            'dimensions': {'x': {'values': [1]}},
            'parameters': {'p_max': {'dims': ['x']}},
            'variables': {'v': {'foreach': ['x'], 'bounds': {'upper': 'p_max'}}},
        }
    )
    assert s.variables['v'].bounds.upper == 'p_max'


@pytest.mark.parametrize(
    ('body', 'match'),
    [
        pytest.param({'foreach': ['x'], 'binary': True, 'integer': True}, 'both binary and integer', id='two-vtypes'),
    ],
)
def test_a_contradictory_declaration_is_rejected(body, match):
    with pytest.raises(ValidationError, match=match):
        MathSchema.model_validate({'dimensions': {'x': {'values': [1]}}, 'variables': {'v': body}})


def test_invalid_sense():
    with pytest.raises(ValidationError, match=r'minimize|maximize'):
        MathSchema.model_validate({'objectives': {'obj': {'sense': 'unknown', 'expression': 'v'}}})


# ---------------------------------------------------------------------------
# a misspelled key is a different model, so it is an error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('raw', 'match'),
    [
        # strictness lives on the shared `_StrictBlock` base, so no model can
        # opt out of it by omission — one row per model to prove it
        pytest.param({'dimenzions': {'x': {'values': [1]}}}, "unknown key 'dimenzions' in the top level", id='top'),
        pytest.param({'dimensions': {'thing': {'dtypo': 'str'}}}, "unknown key 'dtypo'", id='dimension'),
        pytest.param(
            {'dimensions': {'x': {'values': [1]}}, 'parameters': {'thing': {'dims': ['x'], 'dtyp': 'float'}}},
            "unknown key 'dtyp'",
            id='parameter',
        ),
        pytest.param(
            {'dimensions': {'x': {'values': [1]}}, 'macros': {'thing': {'template': 'a + b', 'arg': ['a']}}},
            "unknown key 'arg'",
            id='macro',
        ),
        pytest.param(
            {
                'dimensions': {'x': {'values': [1]}},
                'piecewise': {'thing': {'over': 'x', 'links': [['v', 'p'], ['w', 'q']], 'convx': True}},
            },
            "unknown key 'convx'",
            id='piecewise',
        ),
        pytest.param(
            {'dimensions': {'x': {'values': [1]}}, 'variables': {'v': {'foreach': ['x'], 'bounds': {'lowerr': 0}}}},
            "unknown key 'lowerr' in a bounds block",
            id='nested-bounds',
        ),
        pytest.param(
            {
                'dimensions': {'x': {'values': [1]}},
                'variables': {'v': {'foreach': ['x']}},
                'constraints': {'c': {'foreach': ['x'], 'expresion': 'v >= 0'}},
            },
            "unknown key 'expresion' in a constraint declaration",
            id='nested-constraint',
        ),
    ],
)
def test_an_unknown_key_is_rejected(raw, match):
    with pytest.raises(ValidationError, match=match):
        MathSchema.model_validate(raw)


def test_a_near_miss_is_named_and_anything_else_lists_the_valid_keys():
    """A misspelled key used to be dropped, leaving the variable unbounded —
    so the message has to be good enough to act on without reading the source."""
    base = {'dimensions': {'x': {'values': [1]}}}

    with pytest.raises(ValidationError, match=r"unknown key 'boundz'.*Did you mean 'bounds'"):
        MathSchema.model_validate({**base, 'variables': {'v': {'foreach': ['x'], 'boundz': {'lower': 0}}}})

    with pytest.raises(ValidationError, match='Valid keys: binary, bounds, foreach, integer, where'):
        MathSchema.model_validate({**base, 'variables': {'v': {'foreach': ['x'], 'zzzz': 1}}})


# ---------------------------------------------------------------------------
# dimension coordinates
# ---------------------------------------------------------------------------


def test_coords_list_is_shorthand_for_a_self_named_mapping():
    s = MathSchema.model_validate(
        {'dimensions': {'bus': {'values': ['n']}, 'generator': {'values': ['w'], 'coords': ['bus']}}}
    )
    assert s.dimensions['generator'].coords == {'bus': 'bus'}


def test_coords_mapping_allows_two_coordinates_onto_one_dimension():
    s = MathSchema.model_validate(
        {
            'dimensions': {
                'bus': {'values': ['n']},
                'line': {'values': ['l1'], 'coords': {'from': 'bus', 'to': 'bus'}},
            }
        }
    )
    assert s.dimensions['line'].coords == {'from': 'bus', 'to': 'bus'}


@pytest.mark.parametrize(
    ('dimensions', 'match'),
    [
        pytest.param(
            {'generator': {'values': ['w'], 'coords': ['bus']}},
            "targets undeclared dimension 'bus'",
            id='target-undeclared',
        ),
        pytest.param(
            {'generator': {'values': ['w'], 'coords': {'g': 'generator'}}},
            "targets 'generator' itself",
            id='target-self',
        ),
        pytest.param(
            # `coords: {bus: zone}` would read as a bus coordinate and be a zone one
            {
                'bus': {'values': ['n']},
                'zone': {'values': ['z']},
                'generator': {'values': ['w'], 'coords': {'bus': 'zone'}},
            },
            'shadows the dimension of the same name',
            id='shadows-a-dimension',
        ),
    ],
)
def test_a_coordinate_that_does_not_name_a_target_is_rejected(dimensions, match):
    with pytest.raises(ValidationError, match=match):
        MathSchema.model_validate({'dimensions': dimensions})
