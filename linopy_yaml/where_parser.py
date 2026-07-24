"""pyparsing-based parser for where strings.

Parses strings like ``"p_max > 0 AND NOT is_must_run"`` into an AST
that can be evaluated against an xr.Dataset to produce boolean masks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, assert_never

import numpy as np
import pandas as pd
import pyparsing as pp
import xarray as xr

# ---------------------------------------------------------------------------
# AST nodes
# ---------------------------------------------------------------------------


@dataclass
class BoolLiteral:
    value: bool


@dataclass
class ExistenceCheck:
    """True wherever the named parameter is non-null and finite."""

    name: str


@dataclass
class Comparison:
    name: str
    op: str  # "<=", ">=", "==", "!=", "<", ">"
    value: float | str


@dataclass
class NotNode:
    operand: WhereNode


@dataclass
class AndNode:
    left: WhereNode
    right: WhereNode


@dataclass
class OrNode:
    left: WhereNode
    right: WhereNode


# NOTE: NotNode / AndNode / OrNode reference `WhereNode` in their annotations
# before this line — that works only because `from __future__ import
# annotations` makes annotations strings. Don't remove that future-import
# unless you also reorder these definitions.
WhereNode = BoolLiteral | ExistenceCheck | Comparison | NotNode | AndNode | OrNode


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------

def _build_where_grammar() -> pp.ParserElement:
    """Build and return the pyparsing grammar for where strings."""
    where_expr = pp.Forward()

    # Literals
    true_lit = pp.CaselessKeyword("True").setParseAction(lambda: BoolLiteral(True))
    false_lit = pp.CaselessKeyword("False").setParseAction(lambda: BoolLiteral(False))

    # Numbers
    real = pp.Regex(r"-?\d+\.\d*([eE][+-]?\d+)?").setParseAction(lambda t: float(t[0]))
    integer = pp.Regex(r"-?\d+").setParseAction(lambda t: int(t[0]))
    number = real | integer

    # Names
    name = pp.Regex(r"[a-zA-Z_][a-zA-Z0-9_]*")

    # Comparisons
    comparator = pp.oneOf("<= >= == != < >")
    comparison = (name + comparator + (number | name)).setParseAction(
        lambda t: Comparison(t[0], t[1], t[2])
    )

    # Existence check (bare name)
    existence = name.copy().setParseAction(lambda t: ExistenceCheck(t[0]))

    # Atoms
    atom = (
        true_lit
        | false_lit
        | comparison
        | existence
        | (pp.Suppress("(") + where_expr + pp.Suppress(")"))
    )

    # NOT (highest precedence)
    NOT = pp.CaselessKeyword("NOT").suppress()
    not_expr = (NOT + atom).setParseAction(lambda t: NotNode(t[0])) | atom

    # AND
    AND = pp.CaselessKeyword("AND").suppress()
    and_expr = not_expr + pp.ZeroOrMore(AND + not_expr)
    and_expr.setParseAction(_fold_and)

    # OR (lowest precedence)
    OR = pp.CaselessKeyword("OR").suppress()
    or_expr = and_expr + pp.ZeroOrMore(OR + and_expr)
    or_expr.setParseAction(_fold_or)

    where_expr <<= or_expr
    return where_expr


def _fold_and(tokens: pp.ParseResults) -> Any:
    items = list(tokens)
    result = items[0]
    for item in items[1:]:
        result = AndNode(result, item)
    return result


def _fold_or(tokens: pp.ParseResults) -> Any:
    items = list(tokens)
    result = items[0]
    for item in items[1:]:
        result = OrNode(result, item)
    return result


_WHERE_GRAMMAR = _build_where_grammar()


def parse_where(text: str) -> WhereNode:
    """Parse a where string into an AST."""
    try:
        result = _WHERE_GRAMMAR.parseString(text, parseAll=True)
    except pp.ParseException as e:
        msg = f"Failed to parse where string: {text!r}\n{e}"
        raise ValueError(msg) from e
    return result[0]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate_where(
    text: str | None,
    dataset: xr.Dataset,
    master_coords: dict[str, pd.Index],
) -> xr.DataArray:
    """Evaluate a where string against a parameter dataset.

    Always returns a boolean DataArray mask. Degenerate cases (no where
    string, unknown names, literals) come back 0-dimensional, so callers
    can combine masks with ``&``/``|`` without case analysis.
    """
    if text is None:
        return xr.DataArray(True)

    ast = parse_where(text)
    return _eval_node(ast, dataset, master_coords)


def _eval_node(
    node: WhereNode,
    dataset: xr.Dataset,
    master_coords: dict[str, pd.Index],
) -> xr.DataArray:
    if isinstance(node, BoolLiteral):
        return xr.DataArray(node.value)

    if isinstance(node, ExistenceCheck):
        # Check parameters first
        if node.name in dataset:
            arr = dataset[node.name]
            return arr.notnull() & np.isfinite(arr)
        # Check if it's a dimension name — return all True for that dim
        if node.name in master_coords:
            return xr.DataArray(
                True,
                coords={node.name: master_coords[node.name]},
                dims=[node.name],
            )
        return xr.DataArray(False)

    if isinstance(node, Comparison):
        # Get the array to compare
        if node.name in dataset:
            arr = dataset[node.name]
        elif node.name in master_coords:
            # Dimension coordinate comparison (e.g. "snapshot > 0")
            arr = xr.DataArray(
                master_coords[node.name],
                coords={node.name: master_coords[node.name]},
                dims=[node.name],
            )
        else:
            return xr.DataArray(False)

        val = node.value
        # If val is a string, try to resolve as parameter or keep as string
        if isinstance(val, str):
            if val in dataset:
                val = dataset[val]
            # else keep as string for coordinate comparison

        ops = {
            "==": lambda a, b: a == b,
            "!=": lambda a, b: a != b,
            "<": lambda a, b: a < b,
            ">": lambda a, b: a > b,
            "<=": lambda a, b: a <= b,
            ">=": lambda a, b: a >= b,
        }
        result = ops[node.op](arr, val)
        # NaN propagates as False
        return result.fillna(False).astype(bool)

    if isinstance(node, NotNode):
        return ~_eval_node(node.operand, dataset, master_coords)

    if isinstance(node, AndNode):
        left = _eval_node(node.left, dataset, master_coords)
        right = _eval_node(node.right, dataset, master_coords)
        return left & right

    if isinstance(node, OrNode):
        left = _eval_node(node.left, dataset, master_coords)
        right = _eval_node(node.right, dataset, master_coords)
        return left | right

    assert_never(node)
