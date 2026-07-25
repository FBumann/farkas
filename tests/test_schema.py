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
