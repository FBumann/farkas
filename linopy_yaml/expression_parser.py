"""pyparsing-based expression parser for math expressions.

Parses strings like ``sum(p * cost, over=generator) == load`` into an AST
that can be evaluated against a namespace of linopy variables and xarray
parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pyparsing as pp

# ---------------------------------------------------------------------------
# AST nodes
# ---------------------------------------------------------------------------


@dataclass
class NumberNode:
    value: float


@dataclass
class NameNode:
    name: str


@dataclass
class UnaryOpNode:
    op: str
    operand: ArithNode


@dataclass
class BinOpNode:
    op: str
    left: ArithNode
    right: ArithNode


@dataclass
class FuncCallNode:
    name: str
    args: list[ArithNode] = field(default_factory=list)
    kwargs: dict[str, ArithNode] = field(default_factory=dict)


# An arithmetic-only AST node — no comparison. Nested expression positions
# (operands, args, kwargs) only accept this; CompareNode appears only at the
# top of a parsed expression.
# NOTE: the dataclasses above reference `ArithNode` in their annotations
# before this line — that works only because `from __future__ import
# annotations` makes annotations strings. Don't remove that future-import
# unless you also reorder these definitions.
ArithNode = NumberNode | NameNode | UnaryOpNode | BinOpNode | FuncCallNode


@dataclass
class CompareNode:
    op: str  # "<=", ">=", "=="
    left: ArithNode
    right: ArithNode


ExprNode = ArithNode | CompareNode


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------


def _build_grammar() -> pp.ParserElement:
    """Build and return the pyparsing grammar for math expressions."""
    # Forward reference for recursive arithmetic
    arith = pp.Forward()

    # Atoms
    integer = pp.Regex(r'-?\d+').set_parse_action(lambda t: NumberNode(int(t[0])))
    real = pp.Regex(r'-?\d+\.\d*([eE][+-]?\d+)?').set_parse_action(lambda t: NumberNode(float(t[0])))
    inf_literal = (pp.Literal('.inf') | pp.Literal('inf')).set_parse_action(lambda: NumberNode(float('inf')))
    number = real | inf_literal | integer

    name = pp.Regex(r'[a-zA-Z_][a-zA-Z0-9_]*')

    # Function calls
    kwarg = (name + pp.Suppress('=') + (arith | name)).set_parse_action(lambda t: (t[0], t[1]))
    pos_arg = arith
    arg_list = pp.Optional(pp.DelimitedList(kwarg | pos_arg))
    func_call = (name + pp.Suppress('(') + arg_list + pp.Suppress(')')).set_parse_action(_make_func_call)

    # Atom: function call, number, name, or parenthesized expression
    name_node = name.copy().set_parse_action(lambda t: NameNode(t[0]))
    atom = func_call | number | name_node | (pp.Suppress('(') + arith + pp.Suppress(')'))

    # Unary
    unary = (pp.one_of('+ -') + atom).set_parse_action(lambda t: UnaryOpNode(t[0], t[1])) | atom

    # Power (right-associative)
    power = unary + pp.ZeroOrMore(pp.Literal('**') + unary)
    power.set_parse_action(_make_right_assoc)

    # Multiplication / Division (left-associative)
    mul_div = power + pp.ZeroOrMore(pp.one_of('* /') + power)
    mul_div.set_parse_action(_make_left_assoc)

    # Addition / Subtraction (left-associative)
    add_sub = mul_div + pp.ZeroOrMore(pp.one_of('+ -') + mul_div)
    add_sub.set_parse_action(_make_left_assoc)

    arith <<= add_sub

    # Comparison (optional, at most one)
    comparator = pp.one_of('<= >= ==')
    expr = (arith + comparator + arith).set_parse_action(lambda t: CompareNode(t[1], t[0], t[2])) | arith

    return expr


def _make_func_call(tokens: pp.ParseResults) -> FuncCallNode:
    """Build a FuncCallNode from parsed tokens."""
    name = tokens[0]
    args = []
    kwargs = {}
    for item in tokens[1:]:
        if isinstance(item, tuple) and len(item) == 2:
            k, v = item
            if isinstance(v, str):
                v = NameNode(v) if not v.replace('.', '').isdigit() else NumberNode(float(v))
            kwargs[k] = v
        else:
            args.append(item)
    return FuncCallNode(name=name, args=args, kwargs=kwargs)


def _make_left_assoc(tokens: pp.ParseResults) -> Any:
    """Fold tokens into left-associative BinOpNode chain."""
    items = list(tokens)
    result = items[0]
    i = 1
    while i < len(items):
        op = items[i]
        right = items[i + 1]
        result = BinOpNode(op, result, right)
        i += 2
    return result


def _make_right_assoc(tokens: pp.ParseResults) -> Any:
    """Fold tokens into right-associative BinOpNode chain (for **)."""
    items = list(tokens)
    if len(items) == 1:
        return items[0]
    # Right-associative: a ** b ** c = a ** (b ** c)
    result = items[-1]
    i = len(items) - 3
    while i >= 0:
        op = items[i + 1]
        left = items[i]
        result = BinOpNode(op, left, result)
        i -= 2
    return result


# Module-level compiled grammar
_GRAMMAR = _build_grammar()


def parse_expression(text: str) -> ExprNode:
    """Parse a math expression string into an AST.

    Returns one of: NumberNode, NameNode, UnaryOpNode, BinOpNode,
    CompareNode, or FuncCallNode.
    """
    try:
        result = _GRAMMAR.parse_string(text, parse_all=True)
    except pp.ParseException as e:
        msg = f'Failed to parse expression: {text!r}\n{e}'
        raise ValueError(msg) from e
    return result[0]
