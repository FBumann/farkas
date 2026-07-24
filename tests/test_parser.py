"""Tests for expression and where-string parsers."""

import pandas as pd
import pytest
import xarray as xr

from linopy_yaml.expression_parser import (
    BinOpNode,
    CompareNode,
    FuncCallNode,
    NameNode,
    NumberNode,
    UnaryOpNode,
    parse_expression,
)
from linopy_yaml.where_parser import evaluate_where, parse_where


class TestExpressionParser:
    def test_number(self):
        node = parse_expression("42")
        assert isinstance(node, NumberNode)
        assert node.value == 42

    def test_float(self):
        node = parse_expression("3.14")
        assert isinstance(node, NumberNode)
        assert node.value == pytest.approx(3.14)

    def test_name(self):
        node = parse_expression("p_max")
        assert isinstance(node, NameNode)
        assert node.name == "p_max"

    def test_addition(self):
        node = parse_expression("a + b")
        assert isinstance(node, BinOpNode)
        assert node.op == "+"
        assert isinstance(node.left, NameNode)
        assert isinstance(node.right, NameNode)

    def test_precedence_mul_over_add(self):
        node = parse_expression("a + b * c")
        assert isinstance(node, BinOpNode)
        assert node.op == "+"
        assert isinstance(node.right, BinOpNode)
        assert node.right.op == "*"

    def test_parentheses(self):
        node = parse_expression("(a + b) * c")
        assert isinstance(node, BinOpNode)
        assert node.op == "*"
        assert isinstance(node.left, BinOpNode)
        assert node.left.op == "+"

    def test_comparison(self):
        node = parse_expression("p <= p_max")
        assert isinstance(node, CompareNode)
        assert node.op == "<="

    def test_equality(self):
        node = parse_expression("sum(p, over=g) == load")
        assert isinstance(node, CompareNode)
        assert node.op == "=="

    def test_function_call(self):
        node = parse_expression("sum(p, over=generator)")
        assert isinstance(node, FuncCallNode)
        assert node.name == "sum"
        assert len(node.args) == 1
        assert "over" in node.kwargs

    def test_unary_minus(self):
        node = parse_expression("-x")
        assert isinstance(node, UnaryOpNode)
        assert node.op == "-"

    def test_complex_expression(self):
        node = parse_expression("sum(p * cost, over=generator)")
        assert isinstance(node, FuncCallNode)
        assert isinstance(node.args[0], BinOpNode)

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Failed to parse"):
            parse_expression("a +")


class TestWhereParser:
    def test_bool_literal_true(self):
        from linopy_yaml.where_parser import BoolLiteral

        node = parse_where("True")
        assert isinstance(node, BoolLiteral)
        assert node.value is True

    def test_existence_check(self):
        from linopy_yaml.where_parser import ExistenceCheck

        node = parse_where("p_max")
        assert isinstance(node, ExistenceCheck)
        assert node.name == "p_max"

    def test_comparison(self):
        from linopy_yaml.where_parser import Comparison

        node = parse_where("p_max > 0")
        assert isinstance(node, Comparison)
        assert node.op == ">"
        assert node.value == 0

    def test_and(self):
        from linopy_yaml.where_parser import AndNode

        node = parse_where("a AND b")
        assert isinstance(node, AndNode)

    def test_or(self):
        from linopy_yaml.where_parser import OrNode

        node = parse_where("a OR b")
        assert isinstance(node, OrNode)

    def test_not(self):
        from linopy_yaml.where_parser import NotNode

        node = parse_where("NOT a")
        assert isinstance(node, NotNode)

    def test_precedence_and_over_or(self):
        from linopy_yaml.where_parser import AndNode, OrNode

        node = parse_where("a OR b AND c")
        assert isinstance(node, OrNode)
        assert isinstance(node.right, AndNode)


class TestWhereEvaluation:
    def _ds(self):
        return xr.Dataset(
            {
                "p_max": xr.DataArray(
                    [100, 0, 50],
                    dims=["g"],
                    coords={"g": ["wind", "solar", "gas"]},
                ),
            }
        )

    def _mc(self):
        return {"g": pd.Index(["wind", "solar", "gas"], name="g")}

    def test_none_returns_scalar_true(self):
        mask = evaluate_where(None, self._ds(), self._mc())
        assert isinstance(mask, xr.DataArray)
        assert mask.ndim == 0
        assert bool(mask) is True

    def test_existence_check(self):
        mask = evaluate_where("p_max", self._ds(), self._mc())
        assert isinstance(mask, xr.DataArray)
        assert mask.all()

    def test_comparison(self):
        mask = evaluate_where("p_max > 0", self._ds(), self._mc())
        assert bool(mask.sel(g="wind")) is True
        assert bool(mask.sel(g="solar")) is False
        assert bool(mask.sel(g="gas")) is True

    def test_missing_param_returns_scalar_false(self):
        mask = evaluate_where("nonexistent", self._ds(), self._mc())
        assert isinstance(mask, xr.DataArray)
        assert mask.ndim == 0
        assert bool(mask) is False

    def test_dimension_comparison(self):
        ds = xr.Dataset()
        mc = {"t": pd.Index([0, 1, 2], name="t")}
        mask = evaluate_where("t > 0", ds, mc)
        assert isinstance(mask, xr.DataArray)
        assert bool(mask.sel(t=0)) is False
        assert bool(mask.sel(t=1)) is True
