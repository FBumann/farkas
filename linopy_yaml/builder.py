"""Model builder: schema + data → linopy Model.

Also holds the eager evaluation of every built-in helper. The helper *names*
are the language (``helpers.py``, imported by the linopy-free lane); these
xarray/linopy evaluations are this backend's private business, mirrored on
the relational side by lowering cases and SQL rather than shared code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, assert_never

import xarray as xr

from linopy_yaml._notes import note
from linopy_yaml.expansion import parse_and_expand
from linopy_yaml.expression_parser import (
    ArithNode,
    BinOpNode,
    CompareNode,
    FuncCallNode,
    NameNode,
    NumberNode,
    UnaryOpNode,
)
from linopy_yaml.helpers import unknown_helper_message
from linopy_yaml.where_parser import evaluate_where

if TYPE_CHECKING:
    from collections.abc import Callable

    import linopy
    import pandas as pd

    from linopy_yaml.schema import MathSchema

# Mapping from YAML comparison operators to linopy sign strings
_SIGN_MAP = {'==': '=', '<=': '<=', '>=': '>='}


@dataclass(frozen=True)
class EvalContext:
    """Everything expression evaluation needs to resolve names.

    Grows with the expression language (sub-expression scopes, slice
    bindings, ...) — extend this instead of adding parameters to
    ``_eval_ast`` and every helper-facing seam.
    """

    model: linopy.Model
    dataset: xr.Dataset
    master_coords: dict[str, pd.Index]


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
    _build_variables(model, schema, dataset, master_coords)
    _build_constraints(model, schema, dataset, master_coords)
    _build_objectives(model, schema, dataset, master_coords)


# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------


def _build_variables(
    model: linopy.Model,
    schema: MathSchema,
    dataset: xr.Dataset,
    master_coords: dict[str, pd.Index],
) -> None:
    for vname, vdef in schema.variables.items():
        with note(f"while building variable '{vname}'"):
            coords = {d: master_coords[d] for d in vdef.foreach}

            # Resolve bounds
            lower = _resolve_bound(vdef.bounds.lower, dataset)
            upper = _resolve_bound(vdef.bounds.upper, dataset)

            # Evaluate where mask
            mask = evaluate_where(vdef.where, dataset, master_coords)

            model.add_variables(
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
            raise ValueError(msg)
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


def _build_constraints(
    model: linopy.Model,
    schema: MathSchema,
    dataset: xr.Dataset,
    master_coords: dict[str, pd.Index],
) -> None:
    ctx = EvalContext(model, dataset, master_coords)
    for cname, cdef in schema.constraints.items():
        with note(f"while building constraint '{cname}'"):
            # Evaluate constraint-level where mask
            constraint_mask = evaluate_where(cdef.where, dataset, master_coords)

            n_eqs = len(cdef.equations)

            for i, eq in enumerate(cdef.equations):
                # Per-equation where mask (ANDed with constraint mask)
                eq_mask = evaluate_where(eq.where, dataset, master_coords)
                mask = constraint_mask & eq_mask

                # Parse expression and expand named expressions / macros
                ast = parse_and_expand(eq.expression, schema, f"constraint '{cname}'")
                if not isinstance(ast, CompareNode):
                    msg = (
                        f'Equation {i}: expression must contain exactly one '
                        f'comparison operator (<=, >=, ==).\n'
                        f'Got: {eq.expression!r}'
                    )
                    raise ValueError(msg)

                # Evaluate both sides
                lhs = _eval_ast(ast.left, ctx)
                rhs = _eval_ast(ast.right, ctx)
                sign = _SIGN_MAP[ast.op]

                # Name: single equation uses constraint name directly
                eq_name = cname if n_eqs == 1 else f'{cname}_{i}'

                model.add_constraints(lhs, sign, rhs, name=eq_name, mask=_as_linopy_mask(mask))


# ---------------------------------------------------------------------------
# Objectives
# ---------------------------------------------------------------------------


def _build_objectives(
    model: linopy.Model,
    schema: MathSchema,
    dataset: xr.Dataset,
    master_coords: dict[str, pd.Index],
) -> None:
    ctx = EvalContext(model, dataset, master_coords)
    for oname, odef in schema.objectives.items():
        with note(f"while building objective '{oname}'"):
            eq = odef.equations[0]
            ast = parse_and_expand(eq.expression, schema, f"objective '{oname}'")

            if isinstance(ast, CompareNode):
                msg = f'Expression must not contain a comparison operator. Got: {eq.expression!r}'
                raise ValueError(msg)

            expr = _eval_ast(ast, ctx)

            sense = 'min' if odef.sense == 'minimize' else 'max'
            model.add_objective(expr, overwrite=True, sense=sense)


# ---------------------------------------------------------------------------
# AST evaluation
# ---------------------------------------------------------------------------


def _eval_ast(
    node: ArithNode,
    ctx: EvalContext,
) -> Any:
    """Evaluate an expression AST node against the model namespace."""
    if isinstance(node, NumberNode):
        return node.value

    if isinstance(node, NameNode):
        return _resolve_name(node.name, ctx)

    if isinstance(node, UnaryOpNode):
        operand = _eval_ast(node.operand, ctx)
        if node.op == '-':
            return -operand
        return operand  # unary +

    if isinstance(node, BinOpNode):
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
            raise ValueError(msg)
        return ops[node.op](left, right)

    if isinstance(node, FuncCallNode):
        # validation.py already rejected unknown helpers at load time; this
        # guard covers direct calls that skipped it
        if node.name not in _HELPERS:
            raise NameError(unknown_helper_message(node.name))
        helper = _HELPERS[node.name]
        # Evaluate positional args
        args = [_eval_ast(a, ctx) for a in node.args]
        # Evaluate keyword args — NameNodes become strings (for dim names)
        kwargs = {}
        for k, v in node.kwargs.items():
            if isinstance(v, NameNode):
                kwargs[k] = v.name  # dimension names stay as strings
            else:
                kwargs[k] = _eval_ast(v, ctx)
        return helper(*args, **kwargs)

    assert_never(node)


def _resolve_name(
    name: str,
    ctx: EvalContext,
) -> Any:
    """Resolve a name: check variables first, then parameters."""
    # Check linopy variables. Variables is iterable but defines no __contains__,
    # so `in` would fall back to iteration anyway — materialise once and reuse.
    var_names = list(ctx.model.variables)
    if name in var_names:
        return ctx.model.variables[name]

    # Check parameters
    if name in ctx.dataset:
        return ctx.dataset[name]

    # Helpful error
    param_names = sorted(map(str, ctx.dataset.data_vars))
    msg = (
        f"'{name}' not found.\n"
        f'  Variables:  {var_names}\n'
        f'  Parameters: {param_names}\n'
        f"Check for typos, or ensure '{name}' is declared as a variable "
        f'or parameter.'
    )
    raise NameError(msg)


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
        raise ValueError(msg)

    group = mapping.rename(into)
    if isinstance(array, xr.DataArray) or hasattr(array, 'groupby'):
        return array.groupby(group).sum()
    msg = f"group_sum() does not support type '{type(array).__name__}'."
    raise TypeError(msg)


def _shift_amount(helper: str, kwargs: dict[str, int]) -> tuple[str, int]:
    """Unpack and check the shared ``<dim>=<n>`` signature of roll/shift."""
    if len(kwargs) != 1:
        msg = f'{helper}() expects exactly one keyword argument (dim=n), got {len(kwargs)}: {kwargs}'
        raise TypeError(msg)
    dim, n = next(iter(kwargs.items()))
    if int(n) != n:
        msg = f'{helper}() amount must be an integer, got {n!r}'
        raise TypeError(msg)
    return dim, int(n)


def _helper_shift(array: Any, **kwargs: int) -> Any:
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


def _helper_roll(array: Any, **kwargs: int) -> Any:
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
