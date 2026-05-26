"""Model builder: schema + data → linopy Model."""

from __future__ import annotations

from typing import Any, assert_never

import numpy as np
import pandas as pd
import xarray as xr

import linopy

from linopy_yaml.expression_parser import (
    ArithNode,
    BinOpNode,
    CompareNode,
    FuncCallNode,
    NameNode,
    NumberNode,
    UnaryOpNode,
    parse_expression,
)
from linopy_yaml.helpers import get_helper
from linopy_yaml.schema import MathSchema
from linopy_yaml.where_parser import evaluate_where

# Mapping from YAML comparison operators to linopy sign strings
_SIGN_MAP = {"==": "=", "<=": "<=", ">=": ">="}


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
        try:
            coords = {d: master_coords[d] for d in vdef.foreach}

            # Resolve bounds
            lower = _resolve_bound(vdef.bounds.lower, dataset)
            upper = _resolve_bound(vdef.bounds.upper, dataset)

            # Evaluate where mask
            mask = evaluate_where(vdef.where, dataset, master_coords)
            if isinstance(mask, bool):
                mask = None if mask else xr.DataArray(False)

            model.add_variables(
                lower=lower,
                upper=upper,
                coords=coords,
                name=vname,
                mask=mask,
                binary=vdef.binary,
                integer=vdef.integer,
            )
        except Exception as exc:
            exc.add_note(f"while building variable '{vname}'")
            raise


def _resolve_bound(
    value: float | str,
    dataset: xr.Dataset,
) -> Any:
    """Resolve a bound value — either a literal number or a parameter name."""
    if isinstance(value, str):
        if value not in dataset:
            msg = (
                f"Bound references parameter '{value}' which is not in the "
                f"loaded dataset. Available: {sorted(dataset.data_vars)}"
            )
            raise ValueError(msg)
        return dataset[value]
    return value


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------

def _build_constraints(
    model: linopy.Model,
    schema: MathSchema,
    dataset: xr.Dataset,
    master_coords: dict[str, pd.Index],
) -> None:
    for cname, cdef in schema.constraints.items():
        try:
            # Evaluate constraint-level where mask
            constraint_mask = evaluate_where(cdef.where, dataset, master_coords)

            n_eqs = len(cdef.equations)

            for i, eq in enumerate(cdef.equations):
                # Per-equation where mask (ANDed with constraint mask)
                eq_mask = evaluate_where(eq.where, dataset, master_coords)

                if isinstance(constraint_mask, bool) and isinstance(eq_mask, bool):
                    mask = constraint_mask and eq_mask
                elif isinstance(constraint_mask, bool):
                    mask = eq_mask if constraint_mask else xr.DataArray(False)
                elif isinstance(eq_mask, bool):
                    mask = constraint_mask if eq_mask else xr.DataArray(False)
                else:
                    mask = constraint_mask & eq_mask

                # Parse expression
                ast = parse_expression(eq.expression)
                if not isinstance(ast, CompareNode):
                    msg = (
                        f"Constraint '{cname}' equation {i}: expression must "
                        f"contain exactly one comparison operator (<=, >=, ==).\n"
                        f"Got: {eq.expression!r}"
                    )
                    raise ValueError(msg)

                # Evaluate both sides
                lhs = _eval_ast(ast.left, model, dataset, master_coords)
                rhs = _eval_ast(ast.right, model, dataset, master_coords)
                sign = _SIGN_MAP[ast.op]

                # Name: single equation uses constraint name directly
                eq_name = cname if n_eqs == 1 else f"{cname}_{i}"

                mask_da = None if isinstance(mask, bool) and mask else mask

                model.add_constraints(lhs, sign, rhs, name=eq_name, mask=mask_da)
        except Exception as exc:
            exc.add_note(f"while building constraint '{cname}'")
            raise


# ---------------------------------------------------------------------------
# Objectives
# ---------------------------------------------------------------------------

def _build_objectives(
    model: linopy.Model,
    schema: MathSchema,
    dataset: xr.Dataset,
    master_coords: dict[str, pd.Index],
) -> None:
    for oname, odef in schema.objectives.items():
        try:
            eq = odef.equations[0]
            ast = parse_expression(eq.expression)

            if isinstance(ast, CompareNode):
                msg = (
                    f"Objective '{oname}': expression must not contain a "
                    f"comparison operator. Got: {eq.expression!r}"
                )
                raise ValueError(msg)

            expr = _eval_ast(ast, model, dataset, master_coords)

            sense = "min" if odef.sense == "minimize" else "max"
            model.add_objective(expr, overwrite=True, sense=sense)
        except Exception as exc:
            exc.add_note(f"while building objective '{oname}'")
            raise


# ---------------------------------------------------------------------------
# AST evaluation
# ---------------------------------------------------------------------------

def _eval_ast(
    node: ArithNode,
    model: linopy.Model,
    dataset: xr.Dataset,
    master_coords: dict[str, pd.Index],
) -> Any:
    """Evaluate an expression AST node against the model namespace."""
    if isinstance(node, NumberNode):
        return node.value

    if isinstance(node, NameNode):
        return _resolve_name(node.name, model, dataset, master_coords)

    if isinstance(node, UnaryOpNode):
        operand = _eval_ast(node.operand, model, dataset, master_coords)
        if node.op == "-":
            return -operand
        return operand  # unary +

    if isinstance(node, BinOpNode):
        left = _eval_ast(node.left, model, dataset, master_coords)
        right = _eval_ast(node.right, model, dataset, master_coords)
        ops = {
            "+": lambda a, b: a + b,
            "-": lambda a, b: a - b,
            "*": lambda a, b: a * b,
            "/": lambda a, b: a / b,
            "**": lambda a, b: a**b,
        }
        return ops[node.op](left, right)

    if isinstance(node, FuncCallNode):
        helper = get_helper(node.name)
        # Evaluate positional args
        args = [_eval_ast(a, model, dataset, master_coords) for a in node.args]
        # Evaluate keyword args — NameNodes become strings (for dim names)
        kwargs = {}
        for k, v in node.kwargs.items():
            if isinstance(v, NameNode):
                kwargs[k] = v.name  # dimension names stay as strings
            else:
                kwargs[k] = _eval_ast(v, model, dataset, master_coords)
        return helper(*args, **kwargs)

    assert_never(node)


def _resolve_name(
    name: str,
    model: linopy.Model,
    dataset: xr.Dataset,
    master_coords: dict[str, pd.Index],
) -> Any:
    """Resolve a name: check variables first, then parameters."""
    # Check linopy variables
    if name in model.variables:
        return model.variables[name]

    # Check parameters
    if name in dataset:
        return dataset[name]

    # Helpful error
    var_names = list(model.variables)
    param_names = sorted(dataset.data_vars)
    msg = (
        f"'{name}' not found.\n"
        f"  Variables:  {var_names}\n"
        f"  Parameters: {param_names}\n"
        f"Check for typos, or ensure '{name}' is declared as a variable "
        f"or parameter."
    )
    raise NameError(msg)
