"""Lower a parsed YAML schema (typed AST) to the relational logical-plan IR.

This is the phase-3 seam (SPEC.md §12.2): it consumes the same typed AST the
eager builder evaluates (`expression_parser` / `where_parser` nodes) and emits
a :class:`~linopy_yaml.relational.ir.Program`. It lives on the language side —
the engine subpackage stays free of YAML knowledge, and this module never
imports the eager builder.

v0 language subset: foreach, where, arithmetic (+ - * /), sum, comparison.
`roll`, custom helpers, `**`, and binary/integer variables raise
:class:`~linopy_yaml.relational.executor.RelationalBuildError` with a pointer
to the eager backend.

Semantics mirror the eager builder exactly:
- ``sum(x, over=d)`` where ``x`` does not carry dim ``d`` is a no-op;
- a single-equation constraint keeps the constraint name, multiple equations
  get ``_{i}`` suffixes;
- constraint-level and equation-level where strings are ANDed;
- of multiple objectives, the last one wins; only ``equations[0]`` is used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

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
from linopy_yaml.relational import ir
from linopy_yaml.relational.executor import RelationalBuildError
from linopy_yaml.schema import MathSchema
from linopy_yaml.where_parser import (
    AndNode,
    BoolLiteral,
    Comparison,
    ExistenceCheck,
    NotNode,
    OrNode,
    WhereNode,
    parse_where,
)

if TYPE_CHECKING:
    pass

_SENSES = {"==", "<=", ">="}


def lower_program(schema: MathSchema) -> ir.Program:
    """Compile a validated :class:`MathSchema` into an IR :class:`Program`."""
    parameters = tuple(
        ir.ParameterDecl(name, tuple(pdef.dims))
        for name, pdef in schema.parameters.items()
    )

    variables = []
    for vname, vdef in schema.variables.items():
        if vdef.binary or vdef.integer:
            raise RelationalBuildError(
                f"variable '{vname}': binary/integer variables are not supported "
                f"by the relational backend (v0) — use the eager backend"
            )
        variables.append(
            ir.VariableDecl(
                vname,
                tuple(vdef.foreach),
                where=_lower_where(vdef.where, schema, f"variable '{vname}'"),
                lower=_lower_bound(vdef.bounds.lower),
                upper=_lower_bound(vdef.bounds.upper),
            )
        )

    constraints = []
    for cname, cdef in schema.constraints.items():
        c_where = _lower_where(cdef.where, schema, f"constraint '{cname}'")
        n_eqs = len(cdef.equations)
        for i, eq in enumerate(cdef.equations):
            eq_name = cname if n_eqs == 1 else f"{cname}_{i}"
            eq_where = _lower_where(eq.where, schema, f"constraint '{eq_name}'")
            where = _and_preds(c_where, eq_where)

            ast = parse_expression(eq.expression)
            if not isinstance(ast, CompareNode):
                raise RelationalBuildError(
                    f"constraint '{eq_name}': expression must contain exactly one "
                    f"comparison operator (<=, >=, ==). Got: {eq.expression!r}"
                )
            if ast.op not in _SENSES:
                raise RelationalBuildError(
                    f"constraint '{eq_name}': unsupported sense '{ast.op}'"
                )
            constraints.append(
                ir.ConstraintDecl(
                    eq_name,
                    tuple(cdef.foreach),
                    lhs=_lower_expr(ast.left, schema, f"constraint '{eq_name}'"),
                    sense=ast.op,  # type: ignore[arg-type]
                    rhs=_lower_expr(ast.right, schema, f"constraint '{eq_name}'"),
                    where=where,
                )
            )

    if not schema.objectives:
        raise RelationalBuildError("the relational backend requires an objective")
    oname, odef = next(
        reversed(schema.objectives.items())
    )  # last one wins (eager parity)
    ast = parse_expression(odef.equations[0].expression)
    if isinstance(ast, CompareNode):
        raise RelationalBuildError(
            f"objective '{oname}': expression must not contain a comparison operator"
        )
    objective = ir.ObjectiveDecl(
        "min" if odef.sense == "minimize" else "max",
        _lower_expr(ast, schema, f"objective '{oname}'"),
    )

    return ir.Program(parameters, tuple(variables), tuple(constraints), objective)


def tidy_sources(
    schema: MathSchema,
    data: dict[str, object],
    coords: dict[str, object] | None = None,
) -> dict[str, object]:
    """Adapt the eager path's ``data=``/``coords=`` inputs to executor sources.

    Parameters become tidy DataFrames ``(dims…, value)``; dimension indexes
    come from declared YAML values, ``coords``, or fall back to the executor's
    inference from parameter tables.
    """
    import pandas as pd

    sources: dict[str, object] = {}
    for pname, pdef in schema.parameters.items():
        if pname not in data:
            raise RelationalBuildError(f"no data provided for parameter '{pname}'")
        obj = data[pname]
        if isinstance(obj, pd.Series):
            df = obj.rename("value").rename_axis(pdef.dims).reset_index()
        elif isinstance(obj, pd.DataFrame):
            df = obj
        elif isinstance(obj, (int, float)) and not pdef.dims:
            df = pd.DataFrame({"value": [float(obj)]})
        else:
            raise RelationalBuildError(
                f"parameter '{pname}': cannot adapt {type(obj).__name__} to a tidy "
                f"table — pass a Series indexed by {pdef.dims} or a DataFrame with "
                f"columns {[*pdef.dims, 'value']}"
            )
        sources[pname] = df

    for dname, ddef in schema.dimensions.items():
        if coords and dname in coords:
            idx = pd.Index(coords[dname])  # type: ignore[arg-type]
            sources[dname] = pd.DataFrame({dname: idx})
        elif ddef.values is not None:
            sources[dname] = pd.DataFrame({dname: ddef.values})

    return sources


# ---------------------------------------------------------------------------
# expression lowering
# ---------------------------------------------------------------------------


def _lower_expr(node: ArithNode, schema: MathSchema, context: str) -> ir.Expr:
    if isinstance(node, NumberNode):
        return ir.Const(float(node.value))

    if isinstance(node, NameNode):
        if node.name in schema.variables:
            return ir.Var(node.name)
        if node.name in schema.parameters:
            return ir.Param(node.name)
        raise RelationalBuildError(
            f"{context}: '{node.name}' is neither a declared variable nor parameter"
        )

    if isinstance(node, UnaryOpNode):
        inner = _lower_expr(node.operand, schema, context)
        return ir.Neg(inner) if node.op == "-" else inner

    if isinstance(node, BinOpNode):
        left = _lower_expr(node.left, schema, context)
        right = _lower_expr(node.right, schema, context)
        match node.op:
            case "+":
                return ir.Add(left, right)
            case "-":
                return ir.Add(left, ir.Neg(right))
            case "*":
                return ir.Mul(left, right)
            case "/":
                return ir.Div(left, right)
            case _:
                raise RelationalBuildError(
                    f"{context}: operator '{node.op}' is not supported by the "
                    f"relational backend (v0)"
                )

    if isinstance(node, FuncCallNode):
        if node.name != "sum":
            raise RelationalBuildError(
                f"{context}: helper '{node.name}' is not supported by the relational "
                f"backend (v0 supports only 'sum') — use the eager backend"
            )
        if len(node.args) != 1 or set(node.kwargs) != {"over"}:
            raise RelationalBuildError(
                f"{context}: sum() expects sum(<expr>, over=<dim>)"
            )
        over_node = node.kwargs["over"]
        if not isinstance(over_node, NameNode):
            raise RelationalBuildError(
                f"{context}: sum(over=...) must name a dimension"
            )
        inner = _lower_expr(node.args[0], schema, context)
        # eager parity: summing over a dim the operand does not carry is a no-op
        if over_node.name not in _dims_of(inner, schema):
            return inner
        return ir.Sum(inner, (over_node.name,))

    assert_never(node)


def _dims_of(expr: ir.Expr, schema: MathSchema) -> frozenset[str]:
    if isinstance(expr, ir.Const):
        return frozenset()
    if isinstance(expr, ir.Param):
        return frozenset(schema.parameters[expr.name].dims)
    if isinstance(expr, ir.Var):
        return frozenset(schema.variables[expr.name].foreach)
    if isinstance(expr, ir.Neg):
        return _dims_of(expr.x, schema)
    if isinstance(expr, (ir.Add, ir.Mul, ir.Div)):
        return _dims_of(expr.a, schema) | _dims_of(expr.b, schema)
    if isinstance(expr, ir.Sum):
        return _dims_of(expr.x, schema) - set(expr.over)
    if isinstance(expr, ir.GroupSum):
        d = schema.parameters[expr.mapping].dims[0]
        return (_dims_of(expr.x, schema) - {d}) | {expr.into}
    raise RelationalBuildError(f"cannot infer dims of {type(expr).__name__}")


def _lower_bound(value: float | str) -> ir.Expr:
    if isinstance(value, str):
        return ir.Param(value)
    return ir.Const(float(value))


# ---------------------------------------------------------------------------
# where lowering
# ---------------------------------------------------------------------------


def _lower_where(text: str | None, schema: MathSchema, context: str) -> ir.Pred | None:
    if text is None:
        return None
    pred = _lower_where_node(parse_where(text), schema, context)
    if isinstance(pred, ir.Bool) and pred.value:
        return None  # True is equivalent to no mask
    return pred


def _lower_where_node(node: WhereNode, schema: MathSchema, context: str) -> ir.Pred:
    if isinstance(node, BoolLiteral):
        return ir.Bool(node.value)

    if isinstance(node, ExistenceCheck):
        # eager parity: an existence check on an unknown parameter is False
        if node.name not in schema.parameters:
            return ir.Bool(False)
        return ir.Defined(node.name)

    if isinstance(node, Comparison):
        if node.name in schema.parameters:
            return ir.Cmp(node.name, node.op, node.value)  # type: ignore[arg-type]
        if node.name in schema.dimensions:
            raise RelationalBuildError(
                f"{context}: where-comparisons on dimension '{node.name}' are not "
                f"supported by the relational backend (v0) — use the eager backend"
            )
        raise RelationalBuildError(
            f"{context}: where references '{node.name}', which is not a declared "
            f"parameter"
        )

    if isinstance(node, NotNode):
        return ir.Not(_lower_where_node(node.operand, schema, context))
    if isinstance(node, AndNode):
        return ir.And(
            _lower_where_node(node.left, schema, context),
            _lower_where_node(node.right, schema, context),
        )
    if isinstance(node, OrNode):
        return ir.Or(
            _lower_where_node(node.left, schema, context),
            _lower_where_node(node.right, schema, context),
        )

    assert_never(node)


def _and_preds(a: ir.Pred | None, b: ir.Pred | None) -> ir.Pred | None:
    if a is None:
        return b
    if b is None:
        return a
    return ir.And(a, b)
