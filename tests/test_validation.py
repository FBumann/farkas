"""Tests for load-time validation of expression and where strings."""

from __future__ import annotations

import datetime

import pytest

from farkas.schema import MathSchema
from farkas.validation import validate_expressions
from tests.oracle import farkas_linopy, linopy, pd


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

    def test_objective_with_a_second_equations_entry(self):
        """An objective is one expression, and silently using the first is worse.

        ``constraints.<name>.equations`` being a list means "several constraints
        under one name", so the same key under an objective reads as "several
        terms in one objective" — and used to drop everything past the first,
        giving a wrong answer with nothing to see (#198).
        """
        schema = _schema(
            objectives={
                'cost': {'equations': [{'expression': 'sum(p, over=g)'}, {'expression': 'sum(p_max, over=g)'}]}
            },
        )
        with pytest.raises(ValueError, match="2 entries under 'equations:'") as exc_info:
            validate_expressions(schema)
        # the message names the rewrite, spelled from what was written
        assert 'sum(p, over=g) + sum(p_max, over=g)' in str(exc_info.value)

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

    def test_unknown_name_in_where_is_an_error(self):
        """It used to evaluate to False, which built an empty model in the
        eager lane and raised in the relational one — one language, two
        answers. Resolution makes it a load error for both."""
        schema = _schema(
            constraints={
                'cap': {
                    'foreach': ['g'],
                    'where': 'not_a_param > 0',
                    'equations': [{'expression': 'p <= p_max'}],
                }
            },
        )
        with pytest.raises(ValueError, match="'not_a_param' not found"):
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
        validate_expressions(schema, known_variables={'p': ['g']})

    def test_known_variable_dims_reach_the_objective(self):
        """The dim checker needs an external variable's dims wherever it needs
        the name — objectives included, not just constraints."""
        schema = MathSchema.model_validate(
            {
                'dimensions': {'g': {'values': ['wind', 'solar']}},
                'parameters': {'cost': {'dims': ['g']}},
                'objectives': {'total': {'equations': [{'expression': 'sum(p * cost, over=g)'}]}},
            }
        )
        validate_expressions(schema, known_variables={'p': ['g']})


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
            farkas_linopy.build(f)

    def test_extend_sees_existing_model_variables(self, tmp_path):
        """An extension may reference variables already on the model."""
        model = linopy.Model()
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
        farkas_linopy.extend(model, f)
        assert 'cap' in model.constraints

    def test_extend_flags_unknown_variable(self, tmp_path):
        model = linopy.Model()
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
            farkas_linopy.extend(model, f)


class TestDimensionKwargs:
    """ROADMAP 5c: a dim kwarg that names nothing is a silent no-op, not an error.

    ``sum(p, over=snapshto)`` used to build a model that solved and was wrong —
    both lanes agree on the no-op, so nothing downstream caught it.
    """

    @staticmethod
    def _schema(expression: str, foreach: list[str] | None = None) -> MathSchema:
        # `or` would turn an explicit [] into ['snapshot']; a scalar constraint
        # is a legitimate thing to ask for
        foreach = ['snapshot'] if foreach is None else foreach
        return MathSchema.model_validate(
            {
                'dimensions': {
                    'snapshot': {'dtype': 'int'},
                    # `zone` targets a dim `p` does NOT carry: grouping into one
                    # it already has needs that dim twice, which is its own error
                    'bus': {'values': ['n']},
                    'generator': {'values': ['wind'], 'coords': {'zone': 'bus'}},
                },
                'parameters': {'load': {'dims': ['snapshot']}},
                'variables': {'p': {'foreach': ['snapshot', 'generator']}},
                'constraints': {'c': {'foreach': foreach, 'equations': [{'expression': expression}]}},
            }
        )

    def test_sum_over_typo_is_rejected(self):
        with pytest.raises(ValueError, match='silent no-op') as ei:
            validate_expressions(self._schema('sum(p, over=snapshto) == load'))
        assert 'sum(over=snapshto)' in str(ei.value)

    def test_group_sum_over_typo_is_rejected(self):
        with pytest.raises(ValueError, match='does not name a declared dimension'):
            validate_expressions(self._schema('group_sum(p, over=generatr, by=zone) == load'))

    def test_group_sum_coordinate_typo_is_rejected(self):
        with pytest.raises(ValueError, match="does not name a coordinate of 'generator'"):
            validate_expressions(self._schema('group_sum(p, over=generator, by=zne) == load'))

    def test_roll_and_shift_dim_key_is_checked(self):
        for helper in ('roll', 'shift'):
            with pytest.raises(ValueError, match='does not name a declared dimension'):
                validate_expressions(self._schema(f'{helper}(p, snapshto=1) == load'))

    def test_declared_dimensions_still_pass(self):
        for expression, foreach in (
            ('sum(p, over=generator) == load', ['snapshot']),
            ('group_sum(p, over=generator, by=zone) == load', ['snapshot', 'bus']),
            ('roll(p, snapshot=1) == load', ['snapshot', 'generator']),
            ('shift(p, snapshot=1) == load', ['snapshot', 'generator']),
        ):
            validate_expressions(self._schema(expression, foreach))

    def test_macro_formals_are_not_mistaken_for_dimensions(self):
        """A formal in a dim position is legal inside the template body."""
        schema = MathSchema.model_validate(
            {
                'dimensions': {'generator': {'values': ['wind']}},
                'parameters': {'cost': {'dims': ['generator']}},
                'variables': {'p': {'foreach': ['generator']}},
                'macros': {
                    'ws': {
                        'args': ['array', 'weights'],
                        'kwargs': ['over'],
                        'template': 'sum(array * weights, over=over)',
                    }
                },
                'objectives': {
                    'obj': {'sense': 'minimize', 'equations': [{'expression': 'ws(p, cost, over=generator)'}]}
                },
            }
        )
        validate_expressions(schema)

    @pytest.mark.parametrize(
        ('dtype', 'values', 'match'),
        [
            ('str', [datetime.date(2024, 1, 1)], 'has type date'),
            ('str', [750], 'has type int'),
            ('int', ['alpha'], 'has type str'),
            ('int', [True], 'has type bool'),
        ],
    )
    def test_a_coordinate_must_be_its_declared_dtype(self, dtype, values, match):
        """Nothing checked `values` against `dtype`, so a coordinate YAML had
        resolved to another type failed to join the user's data — and row
        absence is the structural zero, so the model solved a smaller problem.
        """
        schema = _schema(dimensions={'g': {'dtype': dtype, 'values': values}})
        with pytest.raises(ValueError, match=match):
            validate_expressions(schema)

    @pytest.mark.parametrize(
        ('dtype', 'values'),
        [
            ('str', ['no', 'se']),
            ('datetime', [datetime.date(2024, 1, 1)]),
            ('float', [1, 2.5]),
            ('int', [0, 1]),
        ],
    )
    def test_a_coordinate_of_the_declared_dtype_passes(self, dtype, values):
        validate_expressions(_schema(dimensions={'g': {'dtype': dtype, 'values': values}}))

    def test_a_second_objective_is_a_load_error(self):
        """Was: `lowering` took the last declaration and dropped the rest, so a
        file declaring cost and emissions solved for emissions without a word.
        """
        schema = _schema(
            objectives={
                'cost': {'sense': 'minimize', 'equations': [{'expression': 'sum(p, over=g)'}]},
                'emissions': {'sense': 'maximize', 'equations': [{'expression': 'sum(p, over=g)'}]},
            },
        )
        with pytest.raises(ValueError, match='2 objectives declared'):
            validate_expressions(schema)
