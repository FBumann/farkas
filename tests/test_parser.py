"""The two grammars: expression strings and where strings.

Dependency-free by design — these must keep running on a bare install, which
is why nothing here resolves names or touches a backend. A parse result still
holds raw ``NameNode``/``Unresolved*`` nodes; giving them meaning is
``resolution.py``'s job, tested in ``test_resolution.py``.
"""

import pytest

from farkas.expression_parser import (
    BinaryOperatorNode,
    ComparisonNode,
    FunctionCallNode,
    NameNode,
    NumberNode,
    UnaryOperatorNode,
    parse_expression,
)
from farkas.where_parser import (
    AndNode,
    BooleanLiteralNode,
    NotNode,
    OrNode,
    UnresolvedComparisonNode,
    UnresolvedNameNode,
    parse_where,
)


@pytest.mark.parametrize(
    ('text', 'node_type', 'attrs'),
    [
        ('42', NumberNode, {'value': 42}),
        ('3.14', NumberNode, {'value': pytest.approx(3.14)}),
        ('p_max', NameNode, {'name': 'p_max'}),
        ('a + b', BinaryOperatorNode, {'op': '+'}),
        ('-x', UnaryOperatorNode, {'op': '-'}),
        ('p <= p_max', ComparisonNode, {'op': '<='}),
        ('sum(p, over=g) == load', ComparisonNode, {'op': '=='}),
        ('sum(p, over=generator)', FunctionCallNode, {'name': 'sum'}),
    ],
)
def test_an_expression_parses_to_its_node(text, node_type, attrs):
    node = parse_expression(text)
    assert isinstance(node, node_type)
    for attr, expected in attrs.items():
        assert getattr(node, attr) == expected


def test_multiplication_binds_tighter_than_addition():
    node = parse_expression('a + b * c')
    assert node.op == '+'
    assert isinstance(node.right, BinaryOperatorNode)
    assert node.right.op == '*'


def test_parentheses_override_precedence():
    node = parse_expression('(a + b) * c')
    assert node.op == '*'
    assert isinstance(node.left, BinaryOperatorNode)
    assert node.left.op == '+'


def test_a_call_carries_its_positional_and_keyword_arguments():
    node = parse_expression('sum(p * cost, over=generator)')
    assert len(node.args) == 1
    assert isinstance(node.args[0], BinaryOperatorNode)  # the argument is an expression, not just a name
    assert 'over' in node.kwargs


def test_an_unparseable_expression_is_an_error():
    with pytest.raises(ValueError, match='Failed to parse'):
        parse_expression('a +')


@pytest.mark.parametrize(
    ('text', 'node_type', 'attrs'),
    [
        ('True', BooleanLiteralNode, {'value': True}),
        # a bare name is an existence check; what it *names* is resolution's problem
        ('p_max', UnresolvedNameNode, {'name': 'p_max'}),
        ('p_max > 0', UnresolvedComparisonNode, {'op': '>', 'value': 0}),
        ('a AND b', AndNode, {}),
        ('a OR b', OrNode, {}),
        ('NOT a', NotNode, {}),
    ],
)
def test_a_where_string_parses_to_its_node(text, node_type, attrs):
    node = parse_where(text)
    assert isinstance(node, node_type)
    for attr, expected in attrs.items():
        assert getattr(node, attr) == expected


def test_and_binds_tighter_than_or():
    node = parse_where('a OR b AND c')
    assert isinstance(node, OrNode)
    assert isinstance(node.right, AndNode)
