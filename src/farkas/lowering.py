"""Lower a parsed YAML schema (typed AST) to the relational logical plan.

This is the lowering seam (docs/ARCHITECTURE.md, "The relational lane"): it
consumes the same typed AST the
eager builder evaluates (`expression_parser` / `where_parser` nodes) and emits
a :class:`~farkas.relational.plan.Program`. It lives on the language side —
the engine subpackage stays free of YAML knowledge, and this module never
imports the eager builder.

Covered: foreach, where, arithmetic (+ - * /), sum, group_sum, roll, shift,
comparison, and binary/integer variables (variable_type). Constructs with no
lowering raise :class:`~farkas.errors.LanguageError` naming
the construct and its rewrite — never a pointer to another backend: the two
lanes accept the same language, and a rejection here is a language gap
(docs/ROADMAP.md), not a routing decision.

Semantics mirror the eager builder exactly:
- a reduction over a dim the operand does not carry is an error, not a silent
  identity — ``dimensions.py`` owns that rule and this module asks it;
- a single-equation constraint keeps the constraint name, multiple equations
  get ``_{i}`` suffixes;
- constraint-level and equation-level where strings are ANDed;
- a file declares one objective with one ``equations:`` entry — validation
  refuses the rest, so this module reads ``equations[0]`` knowing it is all
  there is;
- an objective sums each term over the dims that term carries, which is what
  term fragments do for free and what the eager lane has to distribute for
  (``builder._objective_expression``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from farkas.dimensions import dims_of
from farkas.errors import LanguageError
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
from farkas.helpers import BUILTIN_NAMES, call_shape_error
from farkas.relational import plan
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
    from farkas.schema import MathSchema

_SENSES = {'==', '<=', '>='}


def lower_program(schema: MathSchema) -> plan.Program:
    """Compile a validated :class:`MathSchema` into a :class:`Program`."""
    from farkas.piecewise import expand_piecewise

    schema = expand_piecewise(schema)
    ns = Namespace.of(schema)
    parameters = tuple(plan.ParameterDeclaration(name, tuple(pdef.dims)) for name, pdef in schema.parameters.items())

    variables = []
    for vname, vdef in schema.variables.items():
        variable_type: plan.VariableType
        if vdef.binary:
            # binary implies fixed 0/1 bounds, matching linopy's binary=True
            variable_type, lower, upper = 'binary', plan.Constant(0.0), plan.Constant(1.0)
        else:
            variable_type = 'integer' if vdef.integer else 'continuous'
            lower, upper = _bound_expression(vdef.bounds.lower), _bound_expression(vdef.bounds.upper)
        variables.append(
            plan.VariableDeclaration(
                vname,
                tuple(vdef.foreach),
                where=_lower_where(vdef.where, ns, f"variable '{vname}'", self_variable=vname),
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
            eq_name = equation_name(cname, i, n_eqs)
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
    oname, odef = next(iter(schema.objectives.items()))
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


def check_core_subset(node: ArithmeticNode, schema: MathSchema, context: str) -> None:
    """Raise :class:`LanguageError` unless *node* has a plan node.

    The subset test *is* the lowering — there is no second definition of what
    the engine accepts, which is what stops the two from drifting. This is the
    same call with the result discarded, offered as a public name so that
    ``piecewise.py`` can check a link expression against the language while
    the error still points at the text the user wrote, rather than at the
    declaration the formulation went on to generate.
    """
    _lower_expr(node, schema, context)


# ---------------------------------------------------------------------------
# expression lowering
# ---------------------------------------------------------------------------


def _lower_expr(node: ArithmeticNode, schema: MathSchema, context: str) -> plan.Expression:
    """Rewrite one resolved core-AST expression as a plan expression.

    Two rules a helper case relies on, neither of them stated here. The call
    shape comes from ``helpers.call_shape_error``, which resolution has already
    applied — it is asked again here so an AST that skipped resolution gets the
    language's wording rather than an ``IndexError``. The dim rules come from
    ``dimensions.dims_of`` over the *core AST*: whether an operand carries the
    dim it is being reduced along is a language question, and lowering asks it
    rather than answering it a second time.

    What stays here is what is genuinely about the plan: which node a call
    becomes, and the shapes a node cannot represent — a ``GroupSum`` groups by
    a declared coordinate, a ``Translate`` distance is an integer literal.
    """
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
            f'(docs/ARCHITECTURE.md hard rule 1).'
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
        if node.name not in BUILTIN_NAMES:
            raise LanguageError(
                f"{context}: helper '{node.name}' has no lowering. The language's "
                f'helpers are {sorted(BUILTIN_NAMES)}; compositions of them '
                f"belong in 'macros:'. Math outside the language belongs in a "
                f"declared 'escape:' island, not in a helper."
            )
        shape_error = call_shape_error(node.name, len(node.args), node.kwargs)
        if shape_error is not None:
            raise LanguageError(f'{context}: {shape_error}')

        if node.name == 'sum':
            over_node = node.kwargs['over']
            if not isinstance(over_node, DimensionNode):
                raise LanguageError(f'{context}: sum(over=...) must name a dimension')
            _check_dim_rules(node, schema, context)
            return plan.Sum(_lower_expr(node.args[0], schema, context), (over_node.name,))

        if node.name == 'group_sum':
            over_node = node.kwargs['over']
            by_node = node.kwargs['by']
            if not isinstance(over_node, DimensionNode):
                raise LanguageError(f'{context}: group_sum(over=...) must name a dimension')
            if not isinstance(by_node, CoordinateNode):
                raise LanguageError(f'{context}: group_sum(by=...) must name a coordinate')
            _check_dim_rules(node, schema, context)
            return plan.GroupSum(
                _lower_expr(node.args[0], schema, context),
                over=over_node.name,
                coordinate=by_node.name,
                into=by_node.into,
            )

        if node.name in ('roll', 'shift'):
            ((dim, shift_node),) = node.kwargs.items()
            sign = 1
            if isinstance(shift_node, UnaryOperatorNode) and shift_node.op == '-':
                sign, shift_node = -1, shift_node.operand
            if not isinstance(shift_node, NumberNode) or int(shift_node.value) != shift_node.value:
                raise LanguageError(f'{context}: {node.name}() shift must be an integer literal')
            _check_dim_rules(node, schema, context)
            return plan.Translate(
                _lower_expr(node.args[0], schema, context),
                dim,
                by=sign * int(shift_node.value),
                wrap=node.name == 'roll',
            )

        raise LanguageError(f"{context}: built-in '{node.name}' declares no lowering case")

    assert_never(node)


def _check_dim_rules(node: FunctionCallNode, schema: MathSchema, context: str) -> None:
    """Apply the language's dim rules to a helper call, discarding the dim set.

    Lowering wants the *raise*, not the answer: ``dimensions`` decides whether
    an operand carries the dim it is being reduced along, and a second copy of
    that decision here is a second thing to keep in step. It is called after
    the plan-shape checks so those get to speak first, and only for the dims of
    one call — the enclosing frame is ``dimensions.check_schema``'s business.
    """
    dims_of(node, schema, context)


def _has_var(expr: plan.Expression) -> bool:
    """Whether *expr* contains a decision variable.

    Degree 1 is the first clause of the expressive ceiling, so it has to be
    decidable without data — that is what makes ``fk.check()`` a real gate.
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


def _bound_expression(value: float | str) -> plan.Expression:
    if isinstance(value, str):
        return plan.Parameter(value)
    return plan.Constant(value)


# ---------------------------------------------------------------------------
# where lowering
# ---------------------------------------------------------------------------


def _lower_where(
    text: str | None, ns: Namespace, context: str, self_variable: str | None = None
) -> plan.Predicate | None:
    node = where_of(text, ns, context, self_variable)
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

    if isinstance(node, VariableDefinedNode):
        return plan.VariableDefined(node.name)

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
    if isinstance(node, (AndNode, OrNode)):
        node_type = plan.And if isinstance(node, AndNode) else plan.Or
        return node_type(
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
