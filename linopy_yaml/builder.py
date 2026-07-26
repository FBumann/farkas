"""Model builder: schema + data → linopy Model.

Also holds the eager evaluation of every built-in helper. The helper *names*
are the language (``helpers.py``, imported by the linopy-free lane); these
xarray/linopy evaluations are this backend's private business, mirrored on
the relational side by lowering cases and SQL rather than shared code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, assert_never

import numpy as np
import xarray as xr

from linopy_yaml._notes import note
from linopy_yaml.errors import DataError, LanguageError
from linopy_yaml.expression_parser import (
    ArithmeticNode,
    BinaryOperatorNode,
    ComparisonNode,
    DimensionNode,
    FunctionCallNode,
    NameNode,
    NumberNode,
    ParameterNode,
    UnaryOperatorNode,
    VariableNode,
)
from linopy_yaml.helpers import unknown_helper_message
from linopy_yaml.resolution import Namespace, expression_of, where_of
from linopy_yaml.where_parser import (
    AndNode,
    BooleanLiteralNode,
    DimensionComparisonNode,
    NotNode,
    OrNode,
    ParameterComparisonNode,
    ParameterDefinedNode,
    UnresolvedComparisonNode,
    UnresolvedNameNode,
    WhereNode,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import linopy
    import pandas as pd

    from linopy_yaml.schema import MathSchema

# Mapping from YAML comparison operators to linopy sign strings
_SIGN_MAP = {'==': '=', '<=': '<=', '>=': '>='}


@dataclass(frozen=True)
class EvaluationContext:
    """Everything expression evaluation needs to resolve names.

    Grows with the expression language (sub-expression scopes, slice
    bindings, ...) — extend this instead of adding parameters to
    ``_eval_ast`` and every helper-facing seam.
    """

    model: linopy.Model
    dataset: xr.Dataset
    master_coords: dict[str, pd.Index]
    schema: MathSchema
    ns: Namespace


def build_model(
    model: linopy.Model,
    schema: MathSchema,
    dataset: xr.Dataset,
    master_coords: dict[str, pd.Index],
) -> None:
    """Populate a linopy Model from a parsed schema and loaded parameters.

    This mutates *model* in-place, adding variables, constraints, and
    objectives as declared in *schema*.
    """
    ctx = EvaluationContext(model, dataset, master_coords, schema, Namespace.of(schema, list(model.variables)))
    _build_variables(ctx)
    _build_constraints(ctx)
    _build_objectives(ctx)


# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------


def _build_variables(ctx: EvaluationContext) -> None:
    for vname, vdef in ctx.schema.variables.items():
        with note(f"while building variable '{vname}'"):
            coords = {d: ctx.master_coords[d] for d in vdef.foreach}

            # Resolve bounds
            lower = _resolve_bound(vdef.bounds.lower, ctx.dataset)
            upper = _resolve_bound(vdef.bounds.upper, ctx.dataset)

            where = where_of(vdef.where, ctx.ns, f"variable '{vname}'")
            mask = evaluate_where(where, ctx.dataset, ctx.master_coords)

            ctx.model.add_variables(
                lower=lower,
                upper=upper,
                coords=coords,
                name=vname,
                mask=_as_linopy_mask(mask),
                binary=vdef.binary,
                integer=vdef.integer,
            )


def _resolve_bound(
    value: float | str,
    dataset: xr.Dataset,
) -> Any:
    """Resolve a bound value — either a literal number or a parameter name."""
    if isinstance(value, str):
        if value not in dataset:
            msg = (
                f"Bound references parameter '{value}' which is not in the "
                f'loaded dataset. Available: {sorted(map(str, dataset.data_vars))}'
            )
            raise DataError(msg)
        return dataset[value]
    return value


def _as_linopy_mask(mask: xr.DataArray) -> xr.DataArray | None:
    """Convert an evaluated where mask to linopy's ``mask=`` argument.

    linopy expects ``None`` for "no mask"; a 0-d True mask means exactly
    that. Everything else (including 0-d False) passes through.
    """
    if mask.ndim == 0 and bool(mask):
        return None
    return mask


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


def _build_constraints(ctx: EvaluationContext) -> None:
    for cname, cdef in ctx.schema.constraints.items():
        with note(f"while building constraint '{cname}'"):
            c_where = where_of(cdef.where, ctx.ns, f"constraint '{cname}'")
            constraint_mask = evaluate_where(c_where, ctx.dataset, ctx.master_coords)

            n_eqs = len(cdef.equations)

            for i, eq in enumerate(cdef.equations):
                eq_name = cname if n_eqs == 1 else f'{cname}_{i}'
                # Per-equation where mask (ANDed with constraint mask)
                eq_where = where_of(eq.where, ctx.ns, f"constraint '{eq_name}'")
                eq_mask = evaluate_where(eq_where, ctx.dataset, ctx.master_coords)
                mask = constraint_mask & eq_mask

                ast = expression_of(eq.expression, ctx.schema, ctx.ns, f"constraint '{eq_name}'")
                if not isinstance(ast, ComparisonNode):
                    msg = (
                        f'Equation {i}: expression must contain exactly one '
                        f'comparison operator (<=, >=, ==).\n'
                        f'Got: {eq.expression!r}'
                    )
                    raise LanguageError(msg)

                # Evaluate both sides
                lhs = _eval_ast(ast.left, ctx)
                rhs = _eval_ast(ast.right, ctx)
                sign = _SIGN_MAP[ast.op]

                ctx.model.add_constraints(lhs, sign, rhs, name=eq_name, mask=_as_linopy_mask(mask))


# ---------------------------------------------------------------------------
# Objectives
# ---------------------------------------------------------------------------


def _build_objectives(ctx: EvaluationContext) -> None:
    for oname, odef in ctx.schema.objectives.items():
        with note(f"while building objective '{oname}'"):
            eq = odef.equations[0]
            ast = expression_of(eq.expression, ctx.schema, ctx.ns, f"objective '{oname}'")

            if isinstance(ast, ComparisonNode):
                msg = f'Expression must not contain a comparison operator. Got: {eq.expression!r}'
                raise LanguageError(msg)

            expr = _eval_ast(ast, ctx)

            sense = 'min' if odef.sense == 'minimize' else 'max'
            ctx.model.add_objective(expr, overwrite=True, sense=sense)


# ---------------------------------------------------------------------------
# AST evaluation
# ---------------------------------------------------------------------------


def _eval_ast(
    node: ArithmeticNode,
    ctx: EvaluationContext,
) -> Any:
    """Evaluate an expression AST node against the model namespace."""
    if isinstance(node, NumberNode):
        return node.value

    if isinstance(node, VariableNode):
        return ctx.model.variables[node.name]

    if isinstance(node, ParameterNode):
        return ctx.dataset[node.name]

    if isinstance(node, (NameNode, DimensionNode)):
        msg = (
            f'{type(node).__name__}({node.name!r}) reached the evaluator. '
            f'Expressions must go through resolution.expression_of() first '
            f'(ARCHITECTURE.md hard rule 1).'
        )
        raise AssertionError(msg)

    if isinstance(node, UnaryOperatorNode):
        operand = _eval_ast(node.operand, ctx)
        if node.op == '-':
            return -operand
        return operand  # unary +

    if isinstance(node, BinaryOperatorNode):
        left = _eval_ast(node.left, ctx)
        right = _eval_ast(node.right, ctx)
        ops = {
            '+': lambda a, b: a + b,
            '-': lambda a, b: a - b,
            '*': lambda a, b: a * b,
            '/': lambda a, b: a / b,
        }
        if node.op not in ops:
            # `**` parses but is not in the language: a variable base breaks
            # degree 1, and a parameters-only power belongs in data prep. The
            # streaming lane rejects it at lowering; this lane must agree.
            msg = (
                f"operator '{node.op}' is not in the language. Multiply the term out, "
                f'or precompute it as a parameter — a variable base would make the '
                f'model nonlinear (see ROADMAP, "The degree axis").'
            )
            raise LanguageError(msg)
        return ops[node.op](left, right)

    if isinstance(node, FunctionCallNode):
        # validation.py already rejected unknown helpers at load time; this
        # guard covers direct calls that skipped it
        if node.name not in _HELPERS:
            raise NameError(unknown_helper_message(node.name))
        helper = _HELPERS[node.name]
        # Evaluate positional args
        args = [_eval_ast(a, ctx) for a in node.args]
        kwargs = {}
        for k, v in node.kwargs.items():
            if isinstance(v, DimensionNode):
                kwargs[k] = v.name
            else:
                kwargs[k] = _eval_ast(v, ctx)
        return helper(*args, **kwargs)

    assert_never(node)


# ---------------------------------------------------------------------------
# Built-in helpers, eager evaluation
# ---------------------------------------------------------------------------
#
# Each operand is an xr.DataArray (a parameter) or a linopy Variable /
# LinearExpression. xarray is imported inside the bodies, not at module level,
# so this module still imports on a bare install — that is what lets
# ``tests/test_architecture.py`` check ``_HELPERS`` against the closed name
# set without the [compat] extra.


def _helper_sum(array: Any, *, over: str) -> Any:
    """Sum *array* over dimension *over*.

    If the array does not have the named dimension, it is returned unchanged.
    """
    if isinstance(array, xr.DataArray):
        if over in array.dims:
            return array.sum(dim=over)
        return array
    if hasattr(array, 'dims') and over in array.dims:
        return array.sum(over)
    return array


def _helper_group_sum(array: Any, mapping: Any, *, into: str) -> Any:
    """Sum *array* through a mapping parameter, producing dimension *into*.

    Usage in YAML: ``group_sum(p, gen_bus, into=bus)``

    *mapping* must be a one-dimensional parameter whose values are group
    labels (e.g. ``gen_bus``: generator → bus). The mapping's dimension is
    summed out; a new dimension named *into* holds the group labels.
    """
    if not isinstance(mapping, xr.DataArray):
        msg = (
            f'group_sum() mapping must be a parameter (got '
            f'{type(mapping).__name__}). Usage: group_sum(expr, mapping, into=dim)'
        )
        raise TypeError(msg)
    if mapping.ndim != 1:
        msg = f'group_sum() mapping must have exactly one dimension, got {list(mapping.dims)}'
        raise LanguageError(msg)

    group = mapping.rename(into)
    if isinstance(array, xr.DataArray) or hasattr(array, 'groupby'):
        return array.groupby(group).sum()
    msg = f"group_sum() does not support type '{type(array).__name__}'."
    raise TypeError(msg)


def _shift_amount(helper: str, kwargs: dict[str, float]) -> tuple[str, int]:
    """Unpack and check the shared ``<dim>=<n>`` signature of roll/shift."""
    if len(kwargs) != 1:
        msg = f'{helper}() expects exactly one keyword argument (dim=n), got {len(kwargs)}: {kwargs}'
        raise TypeError(msg)
    dim, n = next(iter(kwargs.items()))
    if int(n) != n:
        msg = f'{helper}() amount must be an integer, got {n!r}'
        raise TypeError(msg)
    return dim, int(n)


def _helper_shift(array: Any, **kwargs: float) -> Any:
    """Non-cyclic shift along a dimension; vacated positions contribute zero.

    Usage in YAML: ``shift(soc, snapshot=1)`` — the value at *t-1*, with the
    first position empty (an acyclic recurrence, e.g. storage starting empty).
    """
    dim, n = _shift_amount('shift', kwargs)
    if isinstance(array, xr.DataArray):
        return array.shift({dim: n}, fill_value=0)
    if hasattr(array, 'shift'):
        return array.shift({dim: n})
    msg = f"shift() does not support type '{type(array).__name__}'."
    raise TypeError(msg)


def _helper_roll(array: Any, **kwargs: float) -> Any:
    """Roll (circular shift) *array* along a dimension.

    Usage in YAML: ``roll(soc, snapshot=1)``
    """
    dim, n = _shift_amount('roll', kwargs)
    if isinstance(array, xr.DataArray):
        return array.roll({dim: n}, roll_coords=False)
    if hasattr(array, 'roll'):
        return array.roll({dim: n})
    msg = f"roll() does not support type '{type(array).__name__}'."
    raise TypeError(msg)


#: Eager evaluation of every name in ``helpers.BUILTIN_NAMES``. The two must
#: agree exactly — enforced by ``tests/test_architecture.py``, because a name
#: one lane implements and the other does not is precisely the divergence
#: that would make the differential tests a comparison of dialects.
_HELPERS: dict[str, Callable[..., Any]] = {
    'sum': _helper_sum,
    'group_sum': _helper_group_sum,
    'shift': _helper_shift,
    'roll': _helper_roll,
}


# ---------------------------------------------------------------------------
# Where-mask evaluation
# ---------------------------------------------------------------------------
#
# The eager reading of a *resolved* where AST. It lives here rather than in
# where_parser.py because it is xarray-only: the relational lane reads the
# same AST through lowering._lower_where and never wants this code.


def evaluate_where(
    node: WhereNode | None,
    dataset: xr.Dataset,
    master_coords: dict[str, pd.Index],
) -> xr.DataArray:
    """Evaluate a **resolved** where AST against a parameter dataset.

    Takes a node, not a string: resolution (``resolution.resolve_where``) has
    already decided what every name refers to, so this function performs no
    lookups and cannot disagree with the relational lane about scoping.

    Always returns a boolean DataArray mask. The no-mask case comes back
    0-dimensional, so callers combine masks with ``&``/``|`` without case
    analysis.
    """
    if node is None:
        return xr.DataArray(True)

    return _eval_node(node, dataset, master_coords)


def _eval_node(
    node: WhereNode,
    dataset: xr.Dataset,
    master_coords: dict[str, pd.Index],
) -> xr.DataArray:
    if isinstance(node, BooleanLiteralNode):
        return xr.DataArray(node.value)

    if isinstance(node, (UnresolvedNameNode, UnresolvedComparisonNode)):
        msg = (
            f'{type(node).__name__} reached the evaluator unresolved. '
            f'Where strings must go through resolution.resolve_where() first.'
        )
        raise AssertionError(msg)

    if isinstance(node, ParameterDefinedNode):
        arr = dataset[node.name]
        return arr.notnull() & np.isfinite(arr)

    if isinstance(node, (ParameterComparisonNode, DimensionComparisonNode)):
        if isinstance(node, ParameterComparisonNode):
            arr = dataset[node.name]
        else:
            arr = xr.DataArray(
                master_coords[node.name],
                coords={node.name: master_coords[node.name]},
                dims=[node.name],
            )

        val = node.value  # a literal: resolution rejected parameter/variable RHS

        ops = {
            '==': lambda a, b: a == b,
            '!=': lambda a, b: a != b,
            '<': lambda a, b: a < b,
            '>': lambda a, b: a > b,
            '<=': lambda a, b: a <= b,
            '>=': lambda a, b: a >= b,
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
