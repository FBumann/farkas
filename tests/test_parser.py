"""Tests for expression and where-string parsers."""

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
from farkas.where_parser import parse_where


class TestExpressionParser:
    def test_number(self):
        node = parse_expression('42')
        assert isinstance(node, NumberNode)
        assert node.value == 42

    def test_float(self):
        node = parse_expression('3.14')
        assert isinstance(node, NumberNode)
        assert node.value == pytest.approx(3.14)

    def test_name(self):
        node = parse_expression('p_max')
        assert isinstance(node, NameNode)
        assert node.name == 'p_max'

    def test_addition(self):
        node = parse_expression('a + b')
        assert isinstance(node, BinaryOperatorNode)
        assert node.op == '+'
        assert isinstance(node.left, NameNode)
        assert isinstance(node.right, NameNode)

    def test_precedence_mul_over_add(self):
        node = parse_expression('a + b * c')
        assert isinstance(node, BinaryOperatorNode)
        assert node.op == '+'
        assert isinstance(node.right, BinaryOperatorNode)
        assert node.right.op == '*'

    def test_parentheses(self):
        node = parse_expression('(a + b) * c')
        assert isinstance(node, BinaryOperatorNode)
        assert node.op == '*'
        assert isinstance(node.left, BinaryOperatorNode)
        assert node.left.op == '+'

    def test_comparison(self):
        node = parse_expression('p <= p_max')
        assert isinstance(node, ComparisonNode)
        assert node.op == '<='

    def test_equality(self):
        node = parse_expression('sum(p, over=g) == load')
        assert isinstance(node, ComparisonNode)
        assert node.op == '=='

    def test_function_call(self):
        node = parse_expression('sum(p, over=generator)')
        assert isinstance(node, FunctionCallNode)
        assert node.name == 'sum'
        assert len(node.args) == 1
        assert 'over' in node.kwargs

    def test_unary_minus(self):
        node = parse_expression('-x')
        assert isinstance(node, UnaryOperatorNode)
        assert node.op == '-'

    def test_complex_expression(self):
        node = parse_expression('sum(p * cost, over=generator)')
        assert isinstance(node, FunctionCallNode)
        assert isinstance(node.args[0], BinaryOperatorNode)

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match='Failed to parse'):
            parse_expression('a +')


class TestWhereParser:
    def test_bool_literal_true(self):
        from farkas.where_parser import BooleanLiteralNode

        node = parse_where('True')
        assert isinstance(node, BooleanLiteralNode)
        assert node.value is True

    def test_existence_check(self):
        from farkas.where_parser import UnresolvedNameNode

        node = parse_where('p_max')
        assert isinstance(node, UnresolvedNameNode)
        assert node.name == 'p_max'

    def test_comparison(self):
        from farkas.where_parser import UnresolvedComparisonNode

        node = parse_where('p_max > 0')
        assert isinstance(node, UnresolvedComparisonNode)
        assert node.op == '>'
        assert node.value == 0

    def test_and(self):
        from farkas.where_parser import AndNode

        node = parse_where('a AND b')
        assert isinstance(node, AndNode)

    def test_or(self):
        from farkas.where_parser import OrNode

        node = parse_where('a OR b')
        assert isinstance(node, OrNode)

    def test_not(self):
        from farkas.where_parser import NotNode

        node = parse_where('NOT a')
        assert isinstance(node, NotNode)

    def test_precedence_and_over_or(self):
        from farkas.where_parser import AndNode, OrNode

        node = parse_where('a OR b AND c')
        assert isinstance(node, OrNode)
        assert isinstance(node.right, AndNode)
