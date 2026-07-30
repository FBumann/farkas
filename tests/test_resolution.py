"""Name resolution as a pass: one namespace, one answer, both lanes.

The divergences below were all real before resolution moved out of the
backends. Each test names the behaviour each lane used to have.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from lpspec.errors import LanguageError
from lpspec.lowering import lower_program
from lpspec.resolution import Namespace, expression_of, where_of
from lpspec.validation import validate_expressions
from tests.conftest import DISPATCH_MODEL, schema_of

if TYPE_CHECKING:
    from lpspec.schema import MathSchema


def _schema(**overrides) -> MathSchema:
    return schema_of(DISPATCH_MODEL, **overrides)


# ---------------------------------------------------------------------------
# the AST that reaches a backend is fully typed
# ---------------------------------------------------------------------------


def test_no_unresolved_name_survives_the_pass():
    from lpspec.expression_parser import NameNode

    schema = _schema()
    ast = expression_of('sum(p * cost, over=generator) == load', schema, Namespace.of(schema), 't')

    def walk(node):
        assert not isinstance(node, NameNode), f'unresolved {node}'
        for child in vars(node).values():
            if hasattr(child, '__dataclass_fields__'):
                walk(child)
            elif isinstance(child, (list, dict)):
                for item in child.values() if isinstance(child, dict) else child:
                    if hasattr(item, '__dataclass_fields__'):
                        walk(item)

    walk(ast)


def test_names_are_typed_by_kind():
    from lpspec.expression_parser import DimensionNode, ParameterNode, VariableNode

    schema = _schema()
    ast = expression_of('sum(p * cost, over=generator)', schema, Namespace.of(schema), 't')
    assert isinstance(ast.args[0].left, VariableNode)
    assert isinstance(ast.args[0].right, ParameterNode)
    assert isinstance(ast.kwargs['over'], DimensionNode)


def test_dimension_is_not_a_value_in_an_expression():
    schema = _schema()
    with pytest.raises(LanguageError, match='is a dimension'):
        expression_of('p * snapshot', schema, Namespace.of(schema), 't')


# ---------------------------------------------------------------------------
# the divergences resolution removes
# ---------------------------------------------------------------------------


def test_unknown_where_name_is_an_error():
    """Was: scalar-False mask in the eager lane (a model that builds, solves
    and is silently empty); a load error in the relational lane."""
    schema = _schema(**{'variables.p.where': 'typo_name > 0'})
    with pytest.raises(ValueError, match="'typo_name' not found"):
        validate_expressions(schema)
    with pytest.raises(LanguageError, match="'typo_name' not found"):
        lower_program(schema)


def test_parameter_vs_parameter_where_comparison_is_an_error():
    """Was: a parameter comparison in the eager lane, a comparison against the
    string 'cost' in the relational one."""
    schema = _schema(**{'variables.p.where': 'p_max > cost'})
    with pytest.raises(ValueError, match='compares two parameters'):
        validate_expressions(schema)


def test_dimension_vs_dimension_where_comparison_is_an_error():
    """Was: silently empty. The RHS read as the string 'snapshot', so the mask
    compared generator coordinates against another dimension's *name*, matched
    nothing, and the block built with zero rows on both lanes."""
    schema = _schema(**{'variables.p.where': 'generator == snapshot'})
    with pytest.raises(ValueError, match='compares against dimension'):
        validate_expressions(schema)


def test_where_cannot_reference_a_variable():
    schema = _schema(**{'variables.p.where': 'p > 0'})
    with pytest.raises(ValueError, match='built before variables exist'):
        validate_expressions(schema)


def test_string_literal_rhs_still_works():
    """A bare name that is not declared stays a literal — this is how string
    coordinates are compared."""
    schema = _schema(**{'variables.p.where': 'generator == wind'})
    validate_expressions(schema)
    program = lower_program(schema)
    assert program.variables[0].where is not None


# ---------------------------------------------------------------------------
# one flat namespace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('overrides', 'expected'),
    [
        ({'parameters.snapshot': {'dims': []}}, 'collides with the dimension'),
        ({'variables.load': {'foreach': ['snapshot']}}, 'collides with the parameter'),
        ({'parameters.sum': {'dims': []}}, "collides with the built-in helper 'sum'"),
        ({'dimensions.sum': {'values': [1]}}, "collides with the built-in helper 'sum'"),
        ({'variables.generator': {'foreach': ['snapshot']}}, 'collides with the dimension'),
    ],
)
def test_collisions_are_load_errors(overrides, expected):
    with pytest.raises(ValueError, match=expected):
        _schema(**overrides)


def test_declaring_a_parameter_cannot_change_an_existing_where():
    """The hazard the flat namespace exists to prevent: `where: "snapshot > 0"`
    means the dimension, and declaring a parameter of that name must not
    silently reinterpret it — it is rejected instead."""
    schema = _schema(**{'variables.p.where': 'snapshot > 0'})
    assert where_of('snapshot > 0', Namespace.of(schema), 't').__class__.__name__ == 'DimensionComparisonNode'
    with pytest.raises(ValueError, match='collides with the dimension'):
        _schema(**{'variables.p.where': 'snapshot > 0', 'parameters.snapshot': {'dims': []}})


# ---------------------------------------------------------------------------
# macro formals: the one legitimate scope
# ---------------------------------------------------------------------------


def test_macro_formal_may_shadow_a_parameter():
    schema = _schema(**{'macros.scale': {'args': ['cost'], 'template': 'cost * 2'}})
    validate_expressions(schema)


def test_macro_formal_may_not_shadow_a_dimension():
    schema = _schema(**{'macros.agg': {'args': ['x'], 'kwargs': ['generator'], 'template': 'sum(x, over=generator)'}})
    with pytest.raises(ValueError, match="formal 'generator' collides with declared dimension"):
        validate_expressions(schema)


# ---------------------------------------------------------------------------
# bounds are a narrower language than expressions
# ---------------------------------------------------------------------------


def test_bounds_reject_expressions_with_a_message_that_says_so():
    with pytest.raises(ValueError, match='bounds accept a parameter name or a number, not an expression'):
        _schema(**{'variables.p.bounds': {'lower': 0, 'upper': '2 * p_max'}})


def test_bounds_typo_names_the_parameter():
    with pytest.raises(ValueError, match="'p_maxx' is not a declared parameter"):
        _schema(**{'variables.p.bounds': {'lower': 0, 'upper': 'p_maxx'}})
