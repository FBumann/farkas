"""Model builder: schema + data → linopy Model.

Also holds the eager evaluation of every built-in helper. The helper *names*
are the language (``helpers.py``, imported by the linopy-free lane); these
xarray/linopy evaluations are this backend's private business, mirrored on
the relational side by lowering cases and SQL rather than shared code.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, assert_never

import numpy as np
import xarray as xr

from farkas._notes import note
from farkas.errors import DataError, LanguageError
from farkas.expression_parser import (
    ArithmeticNode,
    BinaryOperatorNode,
    ComparisonNode,
    CoordinateNode,
    DimensionNode,
    FunctionCallNode,
    NameNode,
    NumberNode,
    ParameterNode,
    UnaryOperatorNode,
    VariableNode,
)
from farkas.helpers import unknown_helper_message
from farkas.linopy import semantics
from farkas.resolution import Namespace, expression_of, where_of
from farkas.schema import equation_name
from farkas.where_parser import (
    AndNode,
    BooleanLiteralNode,
    DimensionComparisonNode,
    NotNode,
    OrNode,
    ParameterComparisonNode,
    ParameterDefinedNode,
    UnresolvedComparisonNode,
    UnresolvedNameNode,
    VariableDefinedNode,
    WhereNode,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable, Mapping

    import linopy
    import pandas as pd

    from farkas.schema import MathSchema

# Mapping from YAML comparison operators to linopy sign strings
_SIGN_MAP = {'==': '=', '<=': '<=', '>=': '>='}

#: The language's arithmetic. ``**`` is absent on purpose — see ``_eval_ast``.
_ARITHMETIC_OPS: dict[str, Callable[[Any, Any], Any]] = {
    '+': operator.add,
    '-': operator.sub,
    '*': operator.mul,
    '/': operator.truediv,
}

#: Where-comparison operators, evaluated element-wise on a DataArray.
_PREDICATE_OPS: dict[str, Callable[[Any, Any], Any]] = {
    '==': operator.eq,
    '!=': operator.ne,
    '<': operator.lt,
    '>': operator.gt,
    '<=': operator.le,
    '>=': operator.ge,
}


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
    #: dim -> {coordinate name: values as a DataArray over that dim}
    dim_coords: dict[str, dict[str, xr.DataArray]] = field(default_factory=dict)


def build_model(
    model: linopy.Model,
    schema: MathSchema,
    dataset: xr.Dataset,
    master_coords: dict[str, pd.Index],
    dim_coords: dict[str, dict[str, xr.DataArray]] | None = None,
) -> None:
    """Populate a linopy Model from a parsed schema and loaded parameters.

    This mutates *model* in-place, adding variables, constraints, and
    objectives as declared in *schema*.
    """
    ctx = EvaluationContext(
        model,
        dataset,
        master_coords,
        schema,
        Namespace.of(schema, list(model.variables)),
        dim_coords or {},
    )
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

            where = where_of(vdef.where, ctx.ns, f"variable '{vname}'", self_variable=vname)
            mask = evaluate_where(where, ctx.dataset, ctx.master_coords, ctx.model)

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
            constraint_mask = evaluate_where(c_where, ctx.dataset, ctx.master_coords, ctx.model)

            n_eqs = len(cdef.equations)

            for i, eq in enumerate(cdef.equations):
                eq_name = equation_name(cname, i, n_eqs)
                # Per-equation where mask (ANDed with constraint mask)
                eq_where = where_of(eq.where, ctx.ns, f"constraint '{eq_name}'")
                eq_mask = evaluate_where(eq_where, ctx.dataset, ctx.master_coords, ctx.model)
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

            expr = _objective_expression(ast, ctx)

            sense = 'min' if odef.sense == 'minimize' else 'max'
            ctx.model.add_objective(expr, overwrite=True, sense=sense)


def _objective_expression(node: ArithmeticNode, ctx: EvaluationContext) -> Any:
    """*node* as a scalar: each additive term summed over the dims it carries.

    An objective has no ``foreach``, so every dim it names is summed (SPEC §2).
    *Which* dims are summed is per term, not per objective. In
    ``x[i] * a[i] + y[j] * b[j]`` the first term has ``|i|`` summands and the
    second ``|j|``; neither is repeated because its sibling names a dim it does
    not carry. Adding the two operands first — what linopy's ``+`` does —
    broadcasts both to ``(i, j)`` and counts each term once per coordinate of
    the other, so an objective that spans a sparse and a dense variable comes
    out multiplied rather than summed.

    The relational lane never had the problem: an expression there is a set of
    term fragments, each keeping its own dims until the objective sums it. This
    reproduces that by distributing the sum over addition, which is what hard
    rule 3 requires of the two lanes (#197).
    """
    total: Any = None
    for term in _additive_terms(node, ctx):
        # `.sum()` with no dim argument reduces everything the term carries;
        # a bare constant has nothing to reduce and no `.sum` to call.
        scalar = term.sum() if hasattr(term, 'sum') else term
        total = scalar if total is None else total + scalar
    return total


def _additive_terms(node: ArithmeticNode, ctx: EvaluationContext) -> list[Any]:
    """*node* as a list of terms to be summed, multiplication distributed.

    Only the operators that distribute are walked. Everything else is one
    opaque term evaluated the ordinary way — a helper call has already reduced
    whatever it reduces, and its result broadcasts like any other operand.
    Distribution is what keeps ``(x[i] * a[i] + y[j] * b[j]) * c[k]`` two terms
    rather than one broadcast to ``(i, j, k)``.
    """
    if isinstance(node, UnaryOperatorNode) and node.op in {'+', '-'}:
        terms = _additive_terms(node.operand, ctx)
        return [-t for t in terms] if node.op == '-' else terms

    if isinstance(node, BinaryOperatorNode):
        if node.op == '+':
            return _additive_terms(node.left, ctx) + _additive_terms(node.right, ctx)
        if node.op == '-':
            return _additive_terms(node.left, ctx) + [-t for t in _additive_terms(node.right, ctx)]
        if node.op == '*':
            # degree stays whatever it was: a product of two variable-carrying
            # terms still fails in linopy, exactly as it does through _eval_ast
            return [
                left * right for left in _additive_terms(node.left, ctx) for right in _additive_terms(node.right, ctx)
            ]
        if node.op == '/':
            # the divisor carries no variables (degree 1), so it is one value
            divisor = _eval_ast(node.right, ctx)
            return [term / divisor for term in _additive_terms(node.left, ctx)]

    return [_eval_ast(node, ctx)]


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
        return semantics.coefficient(ctx.dataset[node.name])

    if isinstance(node, (NameNode, DimensionNode, CoordinateNode)):
        msg = (
            f'{type(node).__name__}({node.name!r}) reached the evaluator. '
            f'Expressions must go through resolution.expression_of() first '
            f'(docs/ARCHITECTURE.md hard rule 1).'
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
        if node.op not in _ARITHMETIC_OPS:
            # `**` parses but is not in the language: a variable base breaks
            # degree 1, and a parameters-only power belongs in data prep. The
            # streaming lane rejects it at lowering; this lane must agree.
            msg = (
                f"operator '{node.op}' is not in the language. Multiply the term out, "
                f'or precompute it as a parameter — a variable base would make the '
                f'model nonlinear (see ROADMAP, "The degree axis").'
            )
            raise LanguageError(msg)
        return _ARITHMETIC_OPS[node.op](left, right)

    if isinstance(node, FunctionCallNode):
        # validation.py already rejected unknown helpers at load time; this
        # guard covers direct calls that skipped it
        if node.name not in _HELPERS:
            raise NameError(unknown_helper_message(node.name))
        helper = _HELPERS[node.name]
        # Evaluate positional args
        args = [_eval_ast(a, ctx) for a in node.args]
        if node.name == 'group_sum':
            # the coordinate lives on the dimension, not in the parameter
            # dataset, so it is looked up here rather than evaluated as an
            # operand — the helper still sees a plain mapping array
            by = node.kwargs['by']
            assert isinstance(by, CoordinateNode)
            return _helper_group_sum(args[0], _coordinate_array(by, ctx), into=by.into)
        kwargs = {}
        for k, v in node.kwargs.items():
            if isinstance(v, DimensionNode):
                kwargs[k] = v.name
            else:
                kwargs[k] = _eval_ast(v, ctx)
        return helper(*args, **kwargs)

    assert_never(node)


def _coordinate_array(by: CoordinateNode, ctx: EvaluationContext) -> Any:
    """The declared coordinate ``by`` as an array over the dimension carrying it."""
    try:
        return ctx.dim_coords[by.dimension][by.name]
    except KeyError:
        msg = (
            f"coordinate '{by.name}' on dimension '{by.dimension}' has no bound values. "
            f"Pass coords={{'{by.dimension}': <DataFrame with '{by.dimension}' and "
            f"'{by.name}' columns>}}."
        )
        raise DataError(msg) from None


# ---------------------------------------------------------------------------
# Built-in helpers, eager evaluation
# ---------------------------------------------------------------------------
#
# Each operand is an xr.DataArray (a parameter) or a linopy Variable /
# LinearExpression. xarray is imported inside the bodies, not at module level,
# so this module still imports on a bare install — that is what lets
# ``tests/test_architecture.py`` check ``_HELPERS`` against the closed name
# set without the [linopy] extra.


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
    """Sum *array* through a declared coordinate, producing dimension *into*.

    Usage in YAML: ``group_sum(p, over=generator, by=bus)``

    *mapping* is the coordinate's values as a one-dimensional array over the
    dim being grouped (``generator`` → bus labels), supplied by the caller from
    ``EvaluationContext.dim_coords``. That dim is summed out; a new dimension
    named *into* holds the group labels.
    """
    if not isinstance(mapping, xr.DataArray):
        msg = (
            f'group_sum() coordinate must be an array (got '
            f'{type(mapping).__name__}). Usage: group_sum(expr, over=dim, by=coord)'
        )
        raise TypeError(msg)
    if mapping.ndim != 1:
        msg = f'group_sum() mapping must have exactly one dimension, got {list(mapping.dims)}'
        raise LanguageError(msg)

    group = mapping.rename(into)
    # A null coordinate says the label belongs to no group, so its terms
    # contribute nowhere. The relational lane gets that for free — a NULL group
    # key joins no constraint row — but linopy refuses to group by NaN at all,
    # so the members have to be dropped before grouping rather than after.
    present = group.notnull()
    if not bool(present.all()):
        dim = str(group.dims[0])
        group = group.isel({dim: present.to_numpy()})
        array = array.isel({dim: present.to_numpy()})
    if isinstance(array, xr.DataArray) or hasattr(array, 'groupby'):
        return array.groupby(group).sum()
    msg = f"group_sum() does not support type '{type(array).__name__}'."
    raise TypeError(msg)


def _translation(helper: str, kwargs: dict[str, float]) -> Mapping[Hashable, int]:
    """The ``<dim>=<n>`` signature both spellings of ``plan.Translate`` share.

    Only the signature is shared: how far to move is one rule, and whether the
    edge wraps is what makes roll and shift different operators.
    """
    named = {k: v for k, v in kwargs.items() if k != 'fill'}
    if len(named) != 1:
        msg = f'{helper}() expects exactly one keyword argument (dim=n), got {len(named)}: {named}'
        raise TypeError(msg)
    dim, amount = next(iter(named.items()))
    if int(amount) != amount:
        msg = f'{helper}() amount must be an integer, got {amount!r}'
        raise TypeError(msg)
    return {dim: int(amount)}


def _helper_shift(array: Any, **kwargs: float) -> Any:
    """Non-cyclic shift along a dimension.

    Usage in YAML: ``shift(soc, snapshot=1)`` — the value at *t-1*. The vacated
    first position is **absent**, which propagates and drops the row, and is
    what linopy's own v1 convention means by ``.shift()``; ``fill=0`` asks for
    the zero instead (SPEC §7). Nothing is done to the result in the default
    case on purpose: the whole point of #289 was to stop holding linopy off its
    own answer.
    """
    by = _translation('shift', kwargs)
    fill = kwargs.get('fill')
    if isinstance(array, xr.DataArray):
        # A DataArray shift always fills — absence is not representable in
        # data, so lowering refuses a bare shift over a variable-free operand
        # and this branch is only ever reached under `fill=`.
        return array.shift(by, fill_value=fill if fill is not None else np.nan)
    if hasattr(array, 'shift'):
        shifted = array.shift(by)
        return shifted if fill is None else semantics.vacated(shifted, fill)
    msg = f"shift() does not support type '{type(array).__name__}'."
    raise TypeError(msg)


def _helper_roll(array: Any, **kwargs: float) -> Any:
    """Roll (circular shift) *array* along a dimension.

    Usage in YAML: ``roll(soc, snapshot=1)``
    """
    by = _translation('roll', kwargs)
    if isinstance(array, xr.DataArray):
        return array.roll(by, roll_coords=False)
    if hasattr(array, 'roll'):
        return array.roll(by)
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
    model: linopy.Model | None = None,
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

    return _eval_node(node, dataset, master_coords, model)


def _eval_node(
    node: WhereNode,
    dataset: xr.Dataset,
    master_coords: dict[str, pd.Index],
    model: linopy.Model | None = None,
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
        if arr.dtype == bool:
            return arr
        return arr.notnull() & np.isfinite(arr)

    if isinstance(node, VariableDefinedNode):
        if model is None:
            msg = (
                f"where references variable '{node.name}', but no model was passed to the "
                f'evaluator — a variable mask can only be read off the model that holds it.'
            )
            raise AssertionError(msg)
        # A masked-out coordinate carries label -1 (linopy's own marker for an
        # absent slot), which is exactly the question being asked.
        return model.variables[node.name].labels != -1

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
        result = _PREDICATE_OPS[node.op](arr, val)
        # NaN propagates as False
        return result.fillna(False).astype(bool)

    if isinstance(node, NotNode):
        return ~_eval_node(node.operand, dataset, master_coords, model)

    if isinstance(node, AndNode):
        left = _eval_node(node.left, dataset, master_coords, model)
        right = _eval_node(node.right, dataset, master_coords, model)
        return left & right

    if isinstance(node, OrNode):
        left = _eval_node(node.left, dataset, master_coords, model)
        right = _eval_node(node.right, dataset, master_coords, model)
        return left | right

    assert_never(node)
