"""Named sub-expressions and expression macros — YAML-defined, schema-local.

Both are expanded into core AST *before* any backend sees the expression, so
the eager builder and the relational backend support them identically and the
engine contract (core AST is the whole language) is untouched.

Two mechanisms, one substitution engine, zero global state:

- **Named sub-expressions**: the YAML ``expressions:`` block maps a name to
  an expression string. Referencing the name splices in the parsed subtree.
  A named expression is a zero-argument macro.

- **Macros**: the YAML ``macros:`` block declares parameterised expression
  templates — language, not code::

      macros:
        weighted_sum:
          args: [array, weights]
          kwargs: [over]
          template: sum(array * weights, over=over)

  Usage: ``weighted_sum(p, cost, over=generator)``. Formal names shadow
  model names inside the body; everything else resolves against the model
  namespace as usual.

Because macros live in the schema, a YAML file is fully self-contained: its
meaning never depends on Python-side registration state. This also makes
load-time validation complete — every template can be name-checked against
this schema (see ``validation.py``), used or not.

Arbitrary-Python helpers (``@linopy_yaml.register``) remain supported but are
eager-only: they execute against xarray/linopy objects at build time and
cannot be compiled by the relational backend.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, assert_never

from linopy_yaml.expression_parser import (
    ArithNode,
    BinOpNode,
    CompareNode,
    ExprNode,
    FuncCallNode,
    NameNode,
    NumberNode,
    UnaryOpNode,
    parse_expression,
)

if TYPE_CHECKING:
    from linopy_yaml.schema import MacroDef, MathSchema

#: Backstop against pathological nesting the cycle check cannot see.
_MAX_DEPTH = 50


def parse_and_expand(text: str, schema: MathSchema, context: str = 'expression') -> ExprNode:
    """Parse *text* and expand named sub-expressions and macros to core AST."""
    return expand(parse_expression(text), schema, context)


def expand(node: ExprNode, schema: MathSchema, context: str = 'expression') -> ExprNode:
    """Expand all named sub-expressions and macro calls under *node*."""
    if isinstance(node, CompareNode):
        return CompareNode(
            node.op,
            _expand(node.left, schema, context, ()),
            _expand(node.right, schema, context, ()),
        )
    return _expand(node, schema, context, ())


def macro_signature(name: str, macro: MacroDef) -> str:
    """Human-readable call signature, for error messages."""
    parts = [*macro.args, *(f'{k}=...' for k in macro.kwargs)]
    return f'{name}({", ".join(parts)})'


def parse_template(name: str, macro: MacroDef, context: str) -> ArithNode:
    """Parse a macro template, rejecting comparisons."""
    body = parse_expression(macro.template)
    if isinstance(body, CompareNode):
        msg = f"{context}: macro '{name}' template must not contain a comparison operator. Got: {macro.template!r}"
        raise ValueError(msg)
    return body


def _expand(
    node: ArithNode,
    schema: MathSchema,
    context: str,
    stack: tuple[str, ...],
) -> ArithNode:
    if len(stack) > _MAX_DEPTH:
        chain = ' -> '.join(stack)
        msg = f'{context}: expansion exceeds depth {_MAX_DEPTH} (via {chain})'
        raise ValueError(msg)

    if isinstance(node, NumberNode):
        return node

    if isinstance(node, NameNode):
        if node.name in schema.expressions:
            if node.name in stack:
                chain = ' -> '.join([*stack, node.name])
                msg = f'{context}: circular expression reference: {chain}'
                raise ValueError(msg)
            body = _parse_named(node.name, schema, context)
            return _expand(body, schema, context, (*stack, node.name))
        return node

    if isinstance(node, UnaryOpNode):
        return UnaryOpNode(node.op, _expand(node.operand, schema, context, stack))

    if isinstance(node, BinOpNode):
        return BinOpNode(
            node.op,
            _expand(node.left, schema, context, stack),
            _expand(node.right, schema, context, stack),
        )

    if isinstance(node, FuncCallNode):
        if node.name in schema.macros:
            if node.name in stack:
                chain = ' -> '.join([*stack, node.name])
                msg = f'{context}: circular macro reference: {chain}'
                raise ValueError(msg)
            return _expand_macro(node, schema, context, stack)
        # ordinary helper call — expand its arguments
        return FuncCallNode(
            node.name,
            [_expand(a, schema, context, stack) for a in node.args],
            {k: _expand(v, schema, context, stack) for k, v in node.kwargs.items()},
        )

    assert_never(node)


def _parse_named(name: str, schema: MathSchema, context: str) -> ArithNode:
    body = parse_expression(schema.expressions[name])
    if isinstance(body, CompareNode):
        msg = (
            f"{context}: named expression '{name}' must not contain a "
            f'comparison operator. Got: {schema.expressions[name]!r}'
        )
        raise ValueError(msg)
    return body


def _expand_macro(
    call: FuncCallNode,
    schema: MathSchema,
    context: str,
    stack: tuple[str, ...],
) -> ArithNode:
    macro = schema.macros[call.name]
    signature = macro_signature(call.name, macro)
    if len(call.args) != len(macro.args):
        msg = (
            f"{context}: macro '{call.name}' expects {len(macro.args)} "
            f'positional argument(s), got {len(call.args)}. Signature: {signature}'
        )
        raise ValueError(msg)
    if set(call.kwargs) != set(macro.kwargs):
        msg = (
            f"{context}: macro '{call.name}' expects keyword argument(s) "
            f'{sorted(macro.kwargs)}, got {sorted(call.kwargs)}. '
            f'Signature: {signature}'
        )
        raise ValueError(msg)

    # call-by-value: arguments are expanded before substitution, so they may
    # themselves use named expressions and macros
    bindings = {
        **{formal: _expand(arg, schema, context, stack) for formal, arg in zip(macro.args, call.args, strict=False)},
        **{formal: _expand(call.kwargs[formal], schema, context, stack) for formal in macro.kwargs},
    }
    body = parse_template(call.name, macro, context)
    substituted = _substitute(body, bindings)
    # the body may reference named expressions or other macros
    return _expand(substituted, schema, context, (*stack, call.name))


def _substitute(node: ArithNode, bindings: dict[str, ArithNode]) -> ArithNode:
    """Replace formal-name NameNodes in *node* with their bound subtrees."""
    if isinstance(node, NumberNode):
        return node

    if isinstance(node, NameNode):
        if node.name in bindings:
            return copy.deepcopy(bindings[node.name])
        return node

    if isinstance(node, UnaryOpNode):
        return UnaryOpNode(node.op, _substitute(node.operand, bindings))

    if isinstance(node, BinOpNode):
        return BinOpNode(
            node.op,
            _substitute(node.left, bindings),
            _substitute(node.right, bindings),
        )

    if isinstance(node, FuncCallNode):
        return FuncCallNode(
            node.name,
            [_substitute(a, bindings) for a in node.args],
            {k: _substitute(v, bindings) for k, v in node.kwargs.items()},
        )

    assert_never(node)
