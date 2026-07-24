"""Tests for load-time validation of expression and where strings."""

from __future__ import annotations

import pandas as pd
import pytest
from linopy import Model

from linopy_yaml import compat
from linopy_yaml.schema import MathSchema
from linopy_yaml.validation import validate_expressions


def _schema(**overrides) -> MathSchema:
    base = {
        'dimensions': {'g': {'values': ['wind', 'solar']}},
        'parameters': {'p_max': {'dims': ['g']}},
        'variables': {'p': {'foreach': ['g']}},
    }
    base.update(overrides)
    return MathSchema.model_validate(base)


class TestValidateExpressions:
    def test_valid_schema_passes(self):
        schema = _schema(
            constraints={'cap': {'foreach': ['g'], 'equations': [{'expression': 'p <= p_max'}]}},
            objectives={'cost': {'equations': [{'expression': 'sum(p, over=g)'}]}},
        )
        validate_expressions(schema)

    def test_unknown_name_in_constraint(self):
        schema = _schema(
            constraints={'cap': {'foreach': ['g'], 'equations': [{'expression': 'q <= p_max'}]}},
        )
        with pytest.raises(ValueError, match="'q' not found") as exc_info:
            validate_expressions(schema)
        assert "Constraint 'cap'" in str(exc_info.value)
        assert 'p_max' in str(exc_info.value)

    def test_constraint_without_comparison(self):
        schema = _schema(
            constraints={'cap': {'foreach': ['g'], 'equations': [{'expression': 'p + p_max'}]}},
        )
        with pytest.raises(ValueError, match='exactly one comparison'):
            validate_expressions(schema)

    def test_objective_with_comparison(self):
        schema = _schema(
            objectives={'cost': {'equations': [{'expression': 'sum(p, over=g) <= 5'}]}},
        )
        with pytest.raises(ValueError, match='must not contain a comparison'):
            validate_expressions(schema)

    def test_unknown_helper(self):
        schema = _schema(
            objectives={'cost': {'equations': [{'expression': 'frobnicate(p, over=g)'}]}},
        )
        with pytest.raises(ValueError, match="Unknown helper function 'frobnicate'"):
            validate_expressions(schema)

    def test_malformed_where_string(self):
        schema = _schema(
            constraints={
                'cap': {
                    'foreach': ['g'],
                    'where': 'p_max >',
                    'equations': [{'expression': 'p <= p_max'}],
                }
            },
        )
        with pytest.raises(ValueError, match='Failed to parse where string'):
            validate_expressions(schema)

    def test_unknown_name_in_where_is_allowed(self):
        """Unknown names in a where evaluate to False by design — not errors."""
        schema = _schema(
            constraints={
                'cap': {
                    'foreach': ['g'],
                    'where': 'not_a_param > 0',
                    'equations': [{'expression': 'p <= p_max'}],
                }
            },
        )
        validate_expressions(schema)

    def test_dim_name_kwarg_not_flagged(self):
        """Keyword-arg names are dimension names, not data references."""
        schema = _schema(
            objectives={'cost': {'equations': [{'expression': 'sum(p, over=g)'}]}},
        )
        validate_expressions(schema)

    def test_multiple_errors_collected(self):
        schema = _schema(
            constraints={
                'a': {'foreach': ['g'], 'equations': [{'expression': 'q <= 1'}]},
                'b': {'foreach': ['g'], 'equations': [{'expression': 'p + 1'}]},
            },
        )
        with pytest.raises(ValueError) as exc_info:
            validate_expressions(schema)
        msg = str(exc_info.value)
        assert "'q' not found" in msg
        assert 'exactly one comparison' in msg

    def test_known_names_extend_the_namespace(self):
        """extend() passes names from the existing model; they must validate."""
        schema = MathSchema.model_validate(
            {
                'dimensions': {'g': {'values': ['wind', 'solar']}},
                'parameters': {'p_max': {'dims': ['g']}},
                'constraints': {
                    'cap': {
                        'foreach': ['g'],
                        'equations': [{'expression': 'p <= p_max'}],
                    }
                },
            }
        )
        with pytest.raises(ValueError, match="'p' not found"):
            validate_expressions(schema)
        validate_expressions(schema, known_variables=['p'])


class TestLoadTimeIntegration:
    def test_from_yaml_fails_before_data_validation(self, tmp_path):
        """A typo in an expression errors even when data= is absent."""
        f = tmp_path / 'm.yaml'
        f.write_text(
            'dimensions:\n'
            '  g:\n'
            '    values: [wind, solar]\n'
            'variables:\n'
            '  p:\n'
            '    foreach: [g]\n'
            'constraints:\n'
            '  cap:\n'
            '    foreach: [g]\n'
            '    equations:\n'
            '      - expression: pp <= 100\n'
        )
        with pytest.raises(ValueError, match="'pp' not found"):
            compat.build(f)

    def test_extend_sees_existing_model_variables(self, tmp_path):
        """An extension may reference variables already on the model."""
        model = Model()
        model.add_variables(coords={'g': pd.Index(['wind', 'solar'], name='g')}, name='p')

        f = tmp_path / 'ext.yaml'
        f.write_text(
            'dimensions:\n'
            '  g:\n'
            '    values: [wind, solar]\n'
            'constraints:\n'
            '  cap:\n'
            '    foreach: [g]\n'
            '    equations:\n'
            '      - expression: p <= 100\n'
        )
        compat.extend(model, f)
        assert 'cap' in model.constraints

    def test_extend_flags_unknown_variable(self, tmp_path):
        model = Model()
        f = tmp_path / 'ext.yaml'
        f.write_text(
            'dimensions:\n'
            '  g:\n'
            '    values: [wind, solar]\n'
            'constraints:\n'
            '  cap:\n'
            '    foreach: [g]\n'
            '    equations:\n'
            '      - expression: p <= 100\n'
        )
        with pytest.raises(ValueError, match="'p' not found"):
            compat.extend(model, f)
