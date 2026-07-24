"""Model builder: schema + data → linopy Model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, assert_never

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
from linopy_yaml.helpers import get_helper
from linopy_yaml.where_parser import evaluate_where

if TYPE_CHECKING:
    import linopy
    import pandas as pd
    import xarray as xr

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
                f'loaded dataset. Available: {sorted(dataset.data_vars)}'
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
            '**': lambda a, b: a**b,
        }
        return ops[node.op](left, right)

    if isinstance(node, FuncCallNode):
        helper = get_helper(node.name)
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
    # Check linopy variables
    if name in ctx.model.variables:
        return ctx.model.variables[name]

    # Check parameters
    if name in ctx.dataset:
        return ctx.dataset[name]

    # Helpful error
    var_names = list(ctx.model.variables)
    param_names = sorted(ctx.dataset.data_vars)
    msg = (
        f"'{name}' not found.\n"
        f'  Variables:  {var_names}\n'
        f'  Parameters: {param_names}\n'
        f"Check for typos, or ensure '{name}' is declared as a variable "
        f'or parameter.'
    )
    raise NameError(msg)
