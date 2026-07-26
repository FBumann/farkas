"""Tests for YAML schema validation."""

import pytest
from pydantic import ValidationError

from linopy_yaml.schema import MathSchema


def test_empty_schema():
    s = MathSchema.model_validate({})
    assert s.dimensions == {}
    assert s.variables == {}


def test_minimal_schema():
    raw = {
        'dimensions': {'x': {'values': [1, 2, 3]}},
        'parameters': {'a': {'dims': ['x']}},
        'variables': {'v': {'foreach': ['x']}},
    }
    s = MathSchema.model_validate(raw)
    assert 'x' in s.dimensions
    assert s.parameters['a'].dims == ['x']
    assert s.variables['v'].foreach == ['x']


def test_undeclared_dim_in_parameter():
    raw = {
        'dimensions': {'x': {'values': [1]}},
        'parameters': {'a': {'dims': ['y']}},
    }
    with pytest.raises(ValidationError, match="undeclared dimension 'y'"):
        MathSchema.model_validate(raw)


def test_undeclared_dim_in_variable():
    raw = {
        'dimensions': {'x': {'values': [1]}},
        'variables': {'v': {'foreach': ['y']}},
    }
    with pytest.raises(ValidationError, match="undeclared dimension 'y'"):
        MathSchema.model_validate(raw)


def test_undeclared_dim_in_constraint():
    raw = {
        'dimensions': {'x': {'values': [1]}},
        'constraints': {
            'c': {
                'foreach': ['y'],
                'equations': [{'expression': 'v == 0'}],
            }
        },
    }
    with pytest.raises(ValidationError, match="undeclared dimension 'y'"):
        MathSchema.model_validate(raw)


def test_binary_and_integer_conflict():
    raw = {
        'dimensions': {'x': {'values': [1]}},
        'variables': {'v': {'foreach': ['x'], 'binary': True, 'integer': True}},
    }
    with pytest.raises(ValidationError, match='both binary and integer'):
        MathSchema.model_validate(raw)


def test_invalid_sense():
    raw = {
        'objectives': {'obj': {'sense': 'unknown', 'equations': [{'expression': 'v'}]}},
    }
    with pytest.raises(ValidationError, match=r'minimize|maximize'):
        MathSchema.model_validate(raw)


def test_undeclared_bound_parameter():
    raw = {
        'dimensions': {'x': {'values': [1]}},
        'variables': {'v': {'foreach': ['x'], 'bounds': {'upper': 'nonexistent'}}},
    }
    with pytest.raises(ValidationError, match="'nonexistent' is not a declared parameter"):
        MathSchema.model_validate(raw)


def test_bound_parameter_reference_valid():
    raw = {
        'dimensions': {'x': {'values': [1]}},
        'parameters': {'p_max': {'dims': ['x']}},
        'variables': {'v': {'foreach': ['x'], 'bounds': {'upper': 'p_max'}}},
    }
    s = MathSchema.model_validate(raw)
    assert s.variables['v'].bounds.upper == 'p_max'


def test_omitted_bounds_default_to_linopy_s_infinities():
    """A declaration that omits a bound means unbounded, exactly as in
    ``linopy.Model.add_variables`` — never an implicit ``>= 0``.

    Nothing else pins this: both lanes read the same default, so the
    differential tests agree with each other whatever it is.
    """
    from linopy_yaml.lowering import _lower_bound

    s = MathSchema.model_validate({'dimensions': {'x': {'values': [1]}}, 'variables': {'v': {'foreach': ['x']}}})
    bounds = s.variables['v'].bounds
    assert (bounds.lower, bounds.upper) == (float('-inf'), float('inf'))

    # and the relational lane carries it through rather than re-defaulting
    assert _lower_bound(bounds.lower).value == float('-inf')
    assert _lower_bound(bounds.upper).value == float('inf')


def test_unknown_key_in_variable_is_rejected_with_a_suggestion():
    """A misspelled key used to be dropped, leaving the variable unbounded."""
    raw = {
        'dimensions': {'x': {'values': [1]}},
        'variables': {'v': {'foreach': ['x'], 'boundz': {'lower': 0, 'upper': 5}}},
    }
    with pytest.raises(ValidationError, match=r"unknown key 'boundz'.*Did you mean 'bounds'"):
        MathSchema.model_validate(raw)


def test_unknown_key_without_a_near_miss_lists_the_valid_keys():
    raw = {
        'dimensions': {'x': {'values': [1]}},
        'variables': {'v': {'foreach': ['x'], 'zzzz': 1}},
    }
    with pytest.raises(ValidationError, match='Valid keys: binary, bounds, foreach, integer, where'):
        MathSchema.model_validate(raw)


def test_unknown_top_level_section_is_rejected():
    with pytest.raises(ValidationError, match="unknown key 'dimenzions' in the top level"):
        MathSchema.model_validate({'dimenzions': {'x': {'values': [1]}}})


@pytest.mark.parametrize(
    ('section', 'body', 'typo'),
    [
        ('dimensions', {'dtypo': 'str'}, 'dtypo'),
        ('parameters', {'dims': ['x'], 'dtyp': 'float'}, 'dtyp'),
        ('macros', {'template': 'a + b', 'arg': ['a']}, 'arg'),
        ('piecewise', {'over': 'x', 'links': [['v', 'p'], ['w', 'q']], 'convx': True}, 'convx'),
    ],
)
def test_every_schema_model_rejects_unknown_keys(section, body, typo):
    """Strictness is on the shared base, so no model can opt out by omission."""
    raw = {'dimensions': {'x': {'values': [1]}}, section: {'thing': body}}
    with pytest.raises(ValidationError, match=f"unknown key '{typo}'"):
        MathSchema.model_validate(raw)


def test_nested_bounds_block_rejects_unknown_keys():
    raw = {
        'dimensions': {'x': {'values': [1]}},
        'variables': {'v': {'foreach': ['x'], 'bounds': {'lowerr': 0}}},
    }
    with pytest.raises(ValidationError, match="unknown key 'lowerr' in a bounds block"):
        MathSchema.model_validate(raw)


def test_equation_block_rejects_unknown_keys():
    raw = {
        'dimensions': {'x': {'values': [1]}},
        'variables': {'v': {'foreach': ['x']}},
        'constraints': {'c': {'foreach': ['x'], 'equations': [{'expresion': 'v >= 0'}]}},
    }
    with pytest.raises(ValidationError, match="unknown key 'expresion' in an equation"):
        MathSchema.model_validate(raw)


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


def test_a_coordinate_target_must_be_declared():
    raw = {'dimensions': {'generator': {'values': ['w'], 'coords': ['bus']}}}
    with pytest.raises(ValidationError, match="targets undeclared dimension 'bus'"):
        MathSchema.model_validate(raw)


def test_a_coordinate_may_not_target_its_own_dimension():
    raw = {'dimensions': {'generator': {'values': ['w'], 'coords': {'g': 'generator'}}}}
    with pytest.raises(ValidationError, match="targets 'generator' itself"):
        MathSchema.model_validate(raw)


def test_a_coordinate_may_not_shadow_a_different_dimension():
    """`coords: {bus: zone}` would read as a bus coordinate and be a zone one."""
    raw = {
        'dimensions': {
            'bus': {'values': ['n']},
            'zone': {'values': ['z']},
            'generator': {'values': ['w'], 'coords': {'bus': 'zone'}},
        }
    }
    with pytest.raises(ValidationError, match='shadows the dimension of the same name'):
        MathSchema.model_validate(raw)
