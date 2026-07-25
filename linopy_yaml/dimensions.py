"""Static dim-set checking — a type system whose type is a set of dim names.

Parameter ``dims`` are declared, variable ``foreach`` is declared, and helper
dimension arguments are name-checked, so **every node's dim set is computable
before any data is bound**. That is the whole basis of this pass: it runs at
load time, on the resolved core AST, so both lanes get the same answer by
construction rather than by differential test.

The rules::

    Number, Const           -> {}
    Param(p)                -> declared dims
    Var(v)                  -> its foreach
    -x, +x                  -> dims(x)
    a + b, a * b, a / b     -> subset rule: one side's dims must contain the
                               other's; result is the union
    sum(x, over=d)          -> dims(x) - {d};   error if d not in dims(x)
    group_sum(x, m, into=g) -> (dims(x) - dims(m)) | {g};
                               error unless dims(m) subset dims(x)
    roll/shift(x, d=n)      -> dims(x);         error if d not in dims(x)

and at the declaration level::

    constraint  -> dims(lhs) | dims(rhs) must *equal* foreach
    where       -> the predicate's dims must not exceed the frame
    bounds      -> the bound parameter's dims must not exceed foreach

The direction that matters most is the *stray* dim: one the frame does not
declare broadcasts silently in the eager lane and adds coordinate columns in
the relational one, so the same YAML quietly builds a bigger model than it
reads as — exactly what a memory budget cannot absorb. The missing direction
is checked too, since a foreach dim the equation never uses just repeats one
row across it, which is nearly always a typo. Requiring equality costs
nothing: every model in `examples/` and the test suite already satisfies it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from linopy_yaml.expression_parser import (
    ArithNode,
    BinOpNode,
    CompareNode,
    DimRefNode,
    ExprNode,
    FuncCallNode,
    NameNode,
    NumberNode,
    ParamNode,
    UnaryOpNode,
    VarNode,
)
from linopy_yaml.where_parser import (
    AndNode,
    BoolLiteral,
    Comparison,
    DimCmp,
    ExistenceCheck,
    NotNode,
    OrNode,
    ParamCmp,
    ParamDefined,
    WhereNode,
)

if TYPE_CHECKING:
    from linopy_yaml.schema import MathSchema


class DimError(ValueError):
    """A dim-set rule was violated. Raised at load time, before any data."""


def dims_of(node: ExprNode, schema: MathSchema, context: str) -> frozenset[str]:
    """The dim set of a resolved expression, checking every rule on the way."""
    if isinstance(node, CompareNode):
        return _dims(node.left, schema, context) | _dims(node.right, schema, context)
    return _dims(node, schema, context)


def _dims(node: ArithNode, schema: MathSchema, context: str) -> frozenset[str]:
    if isinstance(node, NumberNode):
        return frozenset()

    if isinstance(node, ParamNode):
        return frozenset(schema.parameters[node.name].dims)

    if isinstance(node, VarNode):
        return frozenset(schema.variables[node.name].foreach)

    if isinstance(node, (NameNode, DimRefNode)):
        msg = f'{type(node).__name__} reached the dim checker; resolve the expression first.'
        raise AssertionError(msg)

    if isinstance(node, UnaryOpNode):
        return _dims(node.operand, schema, context)

    if isinstance(node, BinOpNode):
        left = _dims(node.left, schema, context)
        right = _dims(node.right, schema, context)
        if not (left <= right or right <= left):
            raise DimError(
                f'{context}: cannot combine dims {sorted(left)} with {sorted(right)} '
                f"using '{node.op}' — neither side's dims contain the other's, so the "
                f'result would carry {sorted(left | right)} and silently build a larger '
                f'model than either operand. Sum or group one side down first.'
            )
        return left | right

    if isinstance(node, FuncCallNode):
        return _dims_call(node, schema, context)

    assert_never(node)


def _dims_call(node: FuncCallNode, schema: MathSchema, context: str) -> frozenset[str]:
    if node.name == 'sum':
        inner = _dims(node.args[0], schema, context)
        over = node.kwargs['over']
        assert isinstance(over, DimRefNode)
        if over.name not in inner:
            raise DimError(
                f'{context}: sum(over={over.name}) but the expression has dims '
                f'{sorted(inner)}. Summing over a dim the operand does not carry '
                f'is a no-op that builds and solves wrong — drop the sum, or fix '
                f'the dim.'
            )
        return inner - {over.name}

    if node.name == 'group_sum':
        inner = _dims(node.args[0], schema, context)
        mapping = node.args[1]
        into = node.kwargs['into']
        assert isinstance(mapping, ParamNode)
        assert isinstance(into, DimRefNode)
        mdims = frozenset(schema.parameters[mapping.name].dims)
        if not mdims <= inner:
            raise DimError(
                f"{context}: group_sum() mapping '{mapping.name}' has dims "
                f'{sorted(mdims)}, which the expression (dims {sorted(inner)}) does '
                f'not carry.'
            )
        return (inner - mdims) | {into.name}

    if node.name in ('roll', 'shift'):
        inner = _dims(node.args[0], schema, context)
        ((dim, _),) = node.kwargs.items()
        if dim not in inner:
            raise DimError(f"{context}: {node.name}() along '{dim}' but the expression has dims {sorted(inner)}.")
        return inner

    msg = f"{context}: helper '{node.name}' has no dim rule"
    raise DimError(msg)


# ---------------------------------------------------------------------------
# declaration-level rules
# ---------------------------------------------------------------------------


def check_schema(schema: MathSchema) -> None:
    """Check every declaration's dim rules. Raises :class:`DimError`."""
    from linopy_yaml.resolution import Namespace, expression_of, where_of

    ns = Namespace.of(schema)

    for vname, vdef in schema.variables.items():
        frame = frozenset(vdef.foreach)
        context = f"Variable '{vname}'"
        _check_where_dims(where_of(vdef.where, ns, context), schema, frame, context)
        for side in ('lower', 'upper'):
            bound = getattr(vdef.bounds, side)
            if isinstance(bound, str):
                bdims = frozenset(schema.parameters[bound].dims)
                if not bdims <= frame:
                    raise DimError(
                        f"{context}: bounds.{side} parameter '{bound}' has dims "
                        f"{sorted(bdims - frame)} outside the variable's foreach "
                        f'{sorted(frame)}.'
                    )

    for cname, cdef in schema.constraints.items():
        frame = frozenset(cdef.foreach)
        _check_where_dims(where_of(cdef.where, ns, f"Constraint '{cname}'"), schema, frame, f"Constraint '{cname}'")
        n_eqs = len(cdef.equations)
        for i, eq in enumerate(cdef.equations):
            context = f"Constraint '{cname}'" if n_eqs == 1 else f"Constraint '{cname}', equation {i}"
            _check_where_dims(where_of(eq.where, ns, context), schema, frame, context)
            got = dims_of(expression_of(eq.expression, schema, ns, context), schema, context)
            if got != frame:
                stray, missing = sorted(got - frame), sorted(frame - got)
                detail = (
                    f'carries dims {stray} that are not in foreach {sorted(frame)} — every '
                    f'stray dim multiplies the rows this constraint builds; add it to '
                    f'foreach if that is intended, or sum it out'
                    if stray
                    else f'does not carry {missing}, which foreach declares — the same row '
                    f'would be repeated across {missing}; drop it from foreach, or use it '
                    f'in the equation'
                )
                raise DimError(f'{context}: the expression {detail}.')

    for oname, odef in schema.objectives.items():
        context = f"Objective '{oname}'"
        dims_of(expression_of(odef.equations[0].expression, schema, ns, context), schema, context)


def _check_where_dims(
    node: WhereNode | None,
    schema: MathSchema,
    frame: frozenset[str],
    context: str,
) -> None:
    """A predicate may only test dims the frame carries.

    The eager lane used to reduce an outside dim with ``any()`` before
    broadcasting — a mask that fails *open*, silently including everything.
    Both lanes reject it now, and they reject it here, at load time.
    """
    if node is None:
        return

    if isinstance(node, (ParamDefined, ParamCmp)):
        pdims = frozenset(schema.parameters[node.name].dims)
        if not pdims <= frame:
            raise DimError(
                f"{context}: where-parameter '{node.name}' has dims "
                f'{sorted(pdims - frame)} outside the frame {sorted(frame)}. Reducing '
                f'a mask over an unlisted dim would silently widen it.'
            )
    elif isinstance(node, DimCmp):
        if node.name not in frame:
            raise DimError(
                f"{context}: where-comparison on dimension '{node.name}', which is not in the frame {sorted(frame)}."
            )
    elif isinstance(node, NotNode):
        _check_where_dims(node.operand, schema, frame, context)
    elif isinstance(node, (AndNode, OrNode)):
        _check_where_dims(node.left, schema, frame, context)
        _check_where_dims(node.right, schema, frame, context)
    elif isinstance(node, (ExistenceCheck, Comparison)):
        msg = f'{type(node).__name__} reached the dim checker unresolved.'
        raise AssertionError(msg)
    elif not isinstance(node, BoolLiteral):
        assert_never(node)
