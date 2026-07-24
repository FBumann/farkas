"""Named sub-expressions and expression macros.

Both are expanded into core AST *before* any backend sees the expression, so
the eager builder and the relational backend support them identically and the
engine contract (core AST is the whole language) is untouched.

Two mechanisms, one substitution engine:

- **Named sub-expressions** (Layer 1): the YAML ``expressions:`` block maps a
  name to an expression string. Referencing the name splices in the parsed
  subtree. A named expression is a zero-argument macro.

- **Macros** (Layer 2): registered from Python with an expression *template* —
  language, not executable code::

      linopy_yaml.register_macro(
          "weighted_sum", "sum(array * weights, over=over)",
          args=["array", "weights"], kwargs=["over"],
      )

  Usage in YAML: ``weighted_sum(p, cost, over=generator)``. Formal names
  shadow model names inside the body; everything else resolves against the
  model namespace as usual.

Arbitrary-Python helpers (``@linopy_yaml.register``) remain supported but are
eager-only: they execute against xarray/linopy objects at build time and
cannot be compiled by the relational backend.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import assert_never

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
from linopy_yaml.helpers import _REGISTRY as _HELPER_REGISTRY
from linopy_yaml.helpers import BUILTIN_NAMES
from linopy_yaml.schema import MathSchema

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Backstop against pathological nesting the cycle check cannot see
#: (e.g. mutually recursive macros registered after validation).
_MAX_DEPTH = 50


@dataclass(frozen=True)
class MacroDef:
    """A registered expression template."""

    name: str
    params: tuple[str, ...]
    kwparams: tuple[str, ...]
    body: ArithNode


_MACROS: dict[str, MacroDef] = {}


def register_macro(
    name: str,
    template: str,
    *,
    args: list[str] | tuple[str, ...] = (),
    kwargs: list[str] | tuple[str, ...] = (),
) -> None:
    """Register *template* as expression macro *name*.

    The template is parsed at registration, so syntax errors fail here, not
    at first use. Formal names (*args*, *kwargs*) shadow model names inside
    the template body.
    """
    if name in BUILTIN_NAMES or name in _HELPER_REGISTRY:
        msg = (
            f"Cannot register macro '{name}': conflicts with a helper function. "
            f"Helpers: {sorted(BUILTIN_NAMES | set(_HELPER_REGISTRY))}"
        )
        raise ValueError(msg)
    if name in _MACROS:
        msg = f"Macro '{name}' is already registered."
        raise ValueError(msg)
    formals = [*args, *kwargs]
    for formal in [name, *formals]:
        if not _IDENT.match(formal):
            msg = f"'{formal}' is not a valid identifier."
            raise ValueError(msg)
    if len(set(formals)) != len(formals):
        msg = f"Macro '{name}' has duplicate formal names: {formals}"
        raise ValueError(msg)

    body = parse_expression(template)
    if isinstance(body, CompareNode):
        msg = (
            f"Macro '{name}' template must not contain a comparison operator. "
            f"Got: {template!r}"
        )
        raise ValueError(msg)

    _MACROS[name] = MacroDef(name, tuple(args), tuple(kwargs), body)


def unregister_macro(name: str) -> None:
    """Remove a registered macro (primarily for tests)."""
    _MACROS.pop(name, None)


def parse_and_expand(
    text: str, schema: MathSchema, context: str = "expression"
) -> ExprNode:
    """Parse *text* and expand named sub-expressions and macros to core AST."""
    return expand(parse_expression(text), schema, context)


def expand(node: ExprNode, schema: MathSchema, context: str = "expression") -> ExprNode:
    """Expand all named sub-expressions and macro calls under *node*."""
    if isinstance(node, CompareNode):
        return CompareNode(
            node.op,
            _expand(node.left, schema, context, ()),
            _expand(node.right, schema, context, ()),
        )
    return _expand(node, schema, context, ())


def _expand(
    node: ArithNode,
    schema: MathSchema,
    context: str,
    stack: tuple[str, ...],
) -> ArithNode:
    if len(stack) > _MAX_DEPTH:
        chain = " -> ".join(stack)
        msg = f"{context}: expansion exceeds depth {_MAX_DEPTH} (via {chain})"
        raise ValueError(msg)

    if isinstance(node, NumberNode):
        return node

    if isinstance(node, NameNode):
        if node.name in schema.expressions:
            if node.name in stack:
                chain = " -> ".join([*stack, node.name])
                msg = f"{context}: circular expression reference: {chain}"
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
        if node.name in _MACROS:
            if node.name in stack:
                chain = " -> ".join([*stack, node.name])
                msg = f"{context}: circular macro reference: {chain}"
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
            f"comparison operator. Got: {schema.expressions[name]!r}"
        )
        raise ValueError(msg)
    return body


def _expand_macro(
    call: FuncCallNode,
    schema: MathSchema,
    context: str,
    stack: tuple[str, ...],
) -> ArithNode:
    macro = _MACROS[call.name]
    signature = (
        f"{macro.name}({', '.join(macro.params)}"
        f"{', ' if macro.params and macro.kwparams else ''}"
        f"{', '.join(f'{k}=...' for k in macro.kwparams)})"
    )
    if len(call.args) != len(macro.params):
        msg = (
            f"{context}: macro '{macro.name}' expects {len(macro.params)} "
            f"positional argument(s), got {len(call.args)}. Signature: {signature}"
        )
        raise ValueError(msg)
    if set(call.kwargs) != set(macro.kwparams):
        msg = (
            f"{context}: macro '{macro.name}' expects keyword argument(s) "
            f"{sorted(macro.kwparams)}, got {sorted(call.kwargs)}. "
            f"Signature: {signature}"
        )
        raise ValueError(msg)

    # call-by-value: arguments are expanded before substitution, so they may
    # themselves use named expressions and macros
    bindings = {
        **{
            formal: _expand(arg, schema, context, stack)
            for formal, arg in zip(macro.params, call.args)
        },
        **{
            formal: _expand(call.kwargs[formal], schema, context, stack)
            for formal in macro.kwparams
        },
    }
    substituted = _substitute(macro.body, bindings)
    # the body may reference named expressions or other macros
    return _expand(substituted, schema, context, (*stack, macro.name))


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
