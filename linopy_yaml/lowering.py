"""Lower a parsed YAML schema (typed AST) to the relational logical plan.

This is the lowering seam (ARCHITECTURE.md, "The relational lane"): it
consumes the same typed AST the
eager builder evaluates (`expression_parser` / `where_parser` nodes) and emits
a :class:`~linopy_yaml.relational.plan.Program`. It lives on the language side —
the engine subpackage stays free of YAML knowledge, and this module never
imports the eager builder.

Covered: foreach, where, arithmetic (+ - * /), sum, group_sum, roll, shift,
comparison, and binary/integer variables (variable_type). Constructs with no
lowering raise :class:`~linopy_yaml.errors.LanguageError` naming
the construct and its rewrite — never a pointer to another backend: the two
lanes accept the same language, and a rejection here is a language gap
(ROADMAP.md), not a routing decision.

Semantics mirror the eager builder exactly:
- ``sum(x, over=d)`` where ``x`` does not carry dim ``d`` is a no-op;
- a single-equation constraint keeps the constraint name, multiple equations
  get ``_{i}`` suffixes;
- constraint-level and equation-level where strings are ANDed;
- of multiple objectives, the last one wins; only ``equations[0]`` is used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, assert_never

from linopy_yaml.errors import DataError, LanguageError
from linopy_yaml.expression_parser import (
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
from linopy_yaml.helpers import BUILTIN_NAMES
from linopy_yaml.relational import plan
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
    from linopy_yaml.schema import MathSchema

_SENSES = {'==', '<=', '>='}


def lower_program(schema: MathSchema) -> plan.Program:
    """Compile a validated :class:`MathSchema` into a :class:`Program`."""
    from linopy_yaml.piecewise import expand_piecewise

    schema = expand_piecewise(schema)
    ns = Namespace.of(schema)
    parameters = tuple(plan.ParameterDeclaration(name, tuple(pdef.dims)) for name, pdef in schema.parameters.items())

    variables = []
    for vname, vdef in schema.variables.items():
        lower: plan.Expression
        upper: plan.Expression
        if vdef.binary:
            # binary implies fixed 0/1 bounds, matching linopy's binary=True
            variable_type, lower, upper = 'binary', plan.Constant(0.0), plan.Constant(1.0)
        elif vdef.integer:
            variable_type = 'integer'
            lower = _lower_bound(vdef.bounds.lower)
            upper = _lower_bound(vdef.bounds.upper)
        else:
            variable_type = 'continuous'
            lower = _lower_bound(vdef.bounds.lower)
            upper = _lower_bound(vdef.bounds.upper)
        variables.append(
            plan.VariableDeclaration(
                vname,
                tuple(vdef.foreach),
                where=_lower_where(vdef.where, ns, f"variable '{vname}'"),
                lower=lower,
                upper=upper,
                variable_type=variable_type,
            )
        )

    constraints = []
    for cname, cdef in schema.constraints.items():
        c_where = _lower_where(cdef.where, ns, f"constraint '{cname}'")
        n_eqs = len(cdef.equations)
        for i, eq in enumerate(cdef.equations):
            eq_name = cname if n_eqs == 1 else f'{cname}_{i}'
            eq_where = _lower_where(eq.where, ns, f"constraint '{eq_name}'")
            where = _and_preds(c_where, eq_where)

            ast = expression_of(eq.expression, schema, ns, f"constraint '{eq_name}'")
            if not isinstance(ast, ComparisonNode):
                raise LanguageError(
                    f"constraint '{eq_name}': expression must contain exactly one "
                    f'comparison operator (<=, >=, ==). Got: {eq.expression!r}'
                )
            if ast.op not in _SENSES:
                raise LanguageError(f"constraint '{eq_name}': unsupported sense '{ast.op}'")
            constraints.append(
                plan.ConstraintDeclaration(
                    eq_name,
                    tuple(cdef.foreach),
                    lhs=_lower_expr(ast.left, schema, f"constraint '{eq_name}'"),
                    sense=ast.op,
                    rhs=_lower_expr(ast.right, schema, f"constraint '{eq_name}'"),
                    where=where,
                )
            )

    if not schema.objectives:
        raise LanguageError('the relational backend requires an objective')
    oname, odef = next(reversed(schema.objectives.items()))  # last one wins (eager parity)
    ast = expression_of(odef.equations[0].expression, schema, ns, f"objective '{oname}'")
    if isinstance(ast, ComparisonNode):
        raise LanguageError(f"objective '{oname}': expression must not contain a comparison operator")
    objective = plan.ObjectiveDeclaration(
        'min' if odef.sense == 'minimize' else 'max',
        _lower_expr(ast, schema, f"objective '{oname}'"),
    )

    dimensions = tuple(
        plan.DimensionDeclaration(dname, tuple(ddef.coords.items())) for dname, ddef in schema.dimensions.items()
    )
    return plan.Program(parameters, tuple(variables), tuple(constraints), objective, dimensions)


def tidy_sources(
    schema: MathSchema,
    data: dict[str, object],
    coords: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Adapt the eager path's ``data=``/``coords=`` inputs to executor sources.

    Parameters become tidy DataFrames ``(dims…, value)``; dimension indexes
    come from declared YAML values, ``coords``, or fall back to the executor's
    inference from parameter tables.
    """
    import sys
    from pathlib import Path

    import pandas as pd

    # A DataArray argument implies the caller already imported xarray —
    # consult sys.modules instead of importing (keeps the runtime xarray-free)
    xr = sys.modules.get('xarray')

    from linopy_yaml.piecewise import validate_piecewise_data

    # parquet paths cannot be curvature-checked in process; validate the rest
    in_memory = {k: v for k, v in data.items() if not isinstance(v, (str, Path))}
    validate_piecewise_data(schema, in_memory)

    sources: dict[str, object] = {}
    for pname, pdef in schema.parameters.items():
        if pname not in data:
            raise DataError(f"no data provided for parameter '{pname}'")
        obj = data[pname]
        if isinstance(obj, (str, Path)):
            sources[pname] = obj  # parquet path — the executor reads it directly
            continue
        if xr is not None and isinstance(obj, xr.DataArray):
            obj = obj.to_series()
        if isinstance(obj, pd.Series):
            df = obj.rename('value').rename_axis(pdef.dims).reset_index()
        elif isinstance(obj, pd.DataFrame):
            df = obj
        elif isinstance(obj, (int, float)) and not pdef.dims:
            df = pd.DataFrame({'value': [float(obj)]})
        else:
            raise DataError(
                f"parameter '{pname}': cannot adapt {type(obj).__name__} to a tidy "
                f'table — pass a Series indexed by {pdef.dims} or a DataFrame with '
                f'columns {[*pdef.dims, "value"]}'
            )
        sources[pname] = df

    for dname, ddef in schema.dimensions.items():
        if dname in data:
            sources[dname] = data[dname]  # explicit index source (path or frame)
        elif coords and dname in coords:
            src = coords[dname]
            # a frame carries declared coordinate columns alongside the labels;
            # flattening it to an index here would drop them
            sources[dname] = src if isinstance(src, pd.DataFrame) else pd.DataFrame({dname: pd.Index(src)})
        elif ddef.values is not None:
            sources[dname] = pd.DataFrame({dname: ddef.values})

    return sources


# ---------------------------------------------------------------------------
# expression lowering
# ---------------------------------------------------------------------------


def _lower_expr(node: ArithmeticNode, schema: MathSchema, context: str) -> plan.Expression:
    if isinstance(node, NumberNode):
        return plan.Constant(node.value)

    if isinstance(node, VariableNode):
        return plan.Variable(node.name)

    if isinstance(node, ParameterNode):
        return plan.Parameter(node.name)

    if isinstance(node, (NameNode, DimensionNode, CoordinateNode)):
        msg = (
            f'{type(node).__name__}({node.name!r}) reached lowering. Expressions '
            f'must go through resolution.expression_of() first '
            f'(ARCHITECTURE.md hard rule 1).'
        )
        raise AssertionError(msg)

    if isinstance(node, UnaryOperatorNode):
        inner = _lower_expr(node.operand, schema, context)
        return plan.Negate(inner) if node.op == '-' else inner

    if isinstance(node, BinaryOperatorNode):
        left = _lower_expr(node.left, schema, context)
        right = _lower_expr(node.right, schema, context)
        match node.op:
            case '+':
                return plan.Add(left, right)
            case '-':
                return plan.Add(left, plan.Negate(right))
            case '*':
                if _has_var(left) and _has_var(right):
                    raise LanguageError(
                        f'{context}: both factors of a product contain variables, which '
                        f'is degree 2. Multiply the variable by a parameter instead, or '
                        f'model the curve with a piecewise: block — see ROADMAP, '
                        f'"The degree axis".'
                    )
                return plan.Multiply(left, right)
            case '/':
                if _has_var(right):
                    raise LanguageError(
                        f'{context}: the divisor contains variables, which is not affine. '
                        f'Divide by a parameter, or precompute the reciprocal as one.'
                    )
                return plan.Divide(left, right)
            case _:
                raise LanguageError(
                    f"{context}: operator '{node.op}' is not in the language. Multiply the "
                    f'term out, or precompute it as a parameter — a variable base would '
                    f'make the model nonlinear (see ROADMAP, "The degree axis").'
                )

    if isinstance(node, FunctionCallNode):
        if node.name == 'sum':
            if len(node.args) != 1 or set(node.kwargs) != {'over'}:
                raise LanguageError(f'{context}: sum() expects sum(<expr>, over=<dim>)')
            over_node = node.kwargs['over']
            if not isinstance(over_node, DimensionNode):
                raise LanguageError(f'{context}: sum(over=...) must name a dimension')
            inner = _lower_expr(node.args[0], schema, context)
            # eager parity: summing over a dim the operand does not carry is a no-op
            if over_node.name not in _dims_of(inner, schema):
                return inner
            return plan.Sum(inner, (over_node.name,))

        if node.name == 'group_sum':
            if len(node.args) != 1 or set(node.kwargs) != {'over', 'by'}:
                raise LanguageError(f'{context}: group_sum() expects group_sum(<expr>, over=<dim>, by=<coord>)')
            over_node = node.kwargs['over']
            by_node = node.kwargs['by']
            if not isinstance(over_node, DimensionNode):
                raise LanguageError(f'{context}: group_sum(over=...) must name a dimension')
            if not isinstance(by_node, CoordinateNode):
                raise LanguageError(f'{context}: group_sum(by=...) must name a coordinate')
            inner = _lower_expr(node.args[0], schema, context)
            if over_node.name not in _dims_of(inner, schema):
                raise LanguageError(
                    f"{context}: group_sum() over '{over_node.name}' but the expression "
                    f'has dims {sorted(_dims_of(inner, schema))}'
                )
            return plan.GroupSum(inner, over=over_node.name, coordinate=by_node.name, into=by_node.into)

        if node.name in ('roll', 'shift'):
            wrap = node.name == 'roll'
            if len(node.args) != 1 or len(node.kwargs) != 1:
                raise LanguageError(f'{context}: {node.name}() expects {node.name}(<expr>, <dim>=<n>)')
            ((dim, shift_node),) = node.kwargs.items()
            sign = 1
            if isinstance(shift_node, UnaryOperatorNode) and shift_node.op == '-':
                sign, shift_node = -1, shift_node.operand
            if not isinstance(shift_node, NumberNode) or int(shift_node.value) != shift_node.value:
                raise LanguageError(f'{context}: {node.name}() shift must be an integer literal')
            inner = _lower_expr(node.args[0], schema, context)
            if dim not in _dims_of(inner, schema):
                raise LanguageError(
                    f"{context}: {node.name}() along '{dim}' but the expression "
                    f'has dims {sorted(_dims_of(inner, schema))}'
                )
            return plan.Translate(inner, dim, by=sign * int(shift_node.value), wrap=wrap)

        raise LanguageError(
            f"{context}: helper '{node.name}' has no lowering. The language's "
            f'helpers are {sorted(BUILTIN_NAMES)}; compositions of them '
            f"belong in 'macros:'. Math outside the language belongs in a "
            f"declared 'escape:' island, not in a helper."
        )

    assert_never(node)


def _has_var(expr: plan.Expression) -> bool:
    """Whether *expr* contains a decision variable.

    Degree 1 is the first clause of the expressive ceiling, so it has to be
    decidable without data — that is what makes ``ly.check()`` a real gate.
    The executor repeats the check when it compiles terms, for hand-built plans.
    """
    if isinstance(expr, plan.Variable):
        return True
    if isinstance(expr, (plan.Constant, plan.Parameter)):
        return False
    if isinstance(expr, plan.Negate):
        return _has_var(expr.operand)
    if isinstance(expr, (plan.Add, plan.Multiply)):
        return _has_var(expr.left) or _has_var(expr.right)
    if isinstance(expr, plan.Divide):
        return _has_var(expr.numerator) or _has_var(expr.divisor)
    if isinstance(expr, (plan.Sum, plan.Translate, plan.GroupSum)):
        return _has_var(expr.operand)
    raise LanguageError(f'cannot decide degree of {type(expr).__name__}')


def _dims_of(expr: plan.Expression, schema: MathSchema) -> frozenset[str]:
    if isinstance(expr, plan.Constant):
        return frozenset()
    if isinstance(expr, plan.Parameter):
        return frozenset(schema.parameters[expr.name].dims)
    if isinstance(expr, plan.Variable):
        return frozenset(schema.variables[expr.name].foreach)
    if isinstance(expr, plan.Negate):
        return _dims_of(expr.operand, schema)
    if isinstance(expr, (plan.Add, plan.Multiply)):
        return _dims_of(expr.left, schema) | _dims_of(expr.right, schema)
    if isinstance(expr, plan.Divide):
        return _dims_of(expr.numerator, schema) | _dims_of(expr.divisor, schema)
    if isinstance(expr, plan.Sum):
        return _dims_of(expr.operand, schema) - set(expr.over)
    if isinstance(expr, plan.Translate):
        return _dims_of(expr.operand, schema)
    if isinstance(expr, plan.GroupSum):
        return (_dims_of(expr.operand, schema) - {expr.over}) | {expr.into}
    raise LanguageError(f'cannot infer dims of {type(expr).__name__}')


def _lower_bound(value: float | str) -> plan.Expression:
    if isinstance(value, str):
        return plan.Parameter(value)
    return plan.Constant(value)


# ---------------------------------------------------------------------------
# where lowering
# ---------------------------------------------------------------------------


def _lower_where(text: str | None, ns: Namespace, context: str) -> plan.Predicate | None:
    node = where_of(text, ns, context)
    if node is None:
        return None
    pred = _lower_where_node(node, context)
    if isinstance(pred, plan.BooleanConstant) and pred.value:
        return None  # True is equivalent to no mask
    return pred


def _lower_where_node(node: WhereNode, context: str) -> plan.Predicate:
    if isinstance(node, BooleanLiteralNode):
        return plan.BooleanConstant(node.value)

    if isinstance(node, ParameterDefinedNode):
        return plan.ParameterDefined(node.name)

    if isinstance(node, ParameterComparisonNode):
        return plan.ParameterComparison(node.name, node.op, node.value)

    if isinstance(node, DimensionComparisonNode):
        return plan.DimensionComparison(node.name, node.op, node.value)

    if isinstance(node, (UnresolvedNameNode, UnresolvedComparisonNode)):
        msg = (
            f'{type(node).__name__} reached lowering unresolved. Where strings '
            f'must go through resolution.where_of() first.'
        )
        raise AssertionError(msg)

    if isinstance(node, NotNode):
        return plan.Not(_lower_where_node(node.operand, context))
    if isinstance(node, AndNode):
        return plan.And(
            _lower_where_node(node.left, context),
            _lower_where_node(node.right, context),
        )
    if isinstance(node, OrNode):
        return plan.Or(
            _lower_where_node(node.left, context),
            _lower_where_node(node.right, context),
        )

    assert_never(node)


def _and_preds(a: plan.Predicate | None, b: plan.Predicate | None) -> plan.Predicate | None:
    if a is None:
        return b
    if b is None:
        return a
    return plan.And(a, b)
