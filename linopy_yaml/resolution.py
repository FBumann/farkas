"""Name resolution — the pass that makes the core AST fully typed.

Parsers emit ``NameNode``: a token, not yet a meaning. This module rewrites
every one of them into a typed node — :class:`~linopy_yaml.expression_parser.VarNode`,
:class:`~linopy_yaml.expression_parser.ParamNode`, or on the where side a
``ParamCmp`` / ``DimCmp`` — so that **the core AST reaching either backend
contains no unresolved names**.

Why this is a pass and not a backend detail
-------------------------------------------

Resolution used to happen at evaluation time, independently in each lane: the
eager builder looked a name up in the ``linopy.Model``'s variable store and
then in the ``xr.Dataset``; the relational lane looked it up in the schema.
Two implementations of one language rule is a divergence surface, and it had
already diverged in three places:

- an unknown name in a ``where`` was a scalar-``False`` mask in the eager lane
  (a model that builds, solves, and is silently empty) and a load error in the
  relational one;
- ``where: "p_max > other_param"`` compared two parameters in the eager lane
  and compared ``p_max`` against the *string* ``'other_param'`` in the
  relational one;
- a ``where`` name that was neither parameter nor dimension resolved to
  ``False`` rather than being rejected.

With resolution as a pass, those are not bugs to keep fixed in two places —
they are unrepresentable. A backend that receives a ``ParamNode`` cannot
disagree about what it refers to, because the disagreement was resolved before
dispatch (ARCHITECTURE.md hard rule 1: the core AST is the whole contract).

One flat namespace
------------------

Variables, parameters, dimensions, named expressions, macros and built-in
helpers share **one** namespace, and a collision is a load error
(``schema.py``). The alternative — resolving in a fixed order and letting the
winner shadow the loser — makes a file's meaning depend on which kinds of
declaration happen to exist, which is the opposite of the fail-loud contract:
adding a parameter named ``snapshot`` would silently change what an existing
``where: "snapshot > 0"`` means.

Macro formals are the one legitimate scope. They shadow model names inside
the template body (call-by-value expansion avoids capture), but they may not
collide with a **declared dimension** — a formal named ``snapshot`` inside a
template that also writes ``over=snapshot`` would be ambiguous with no way to
say which was meant.
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
from linopy_yaml.helpers import BUILTIN_NAMES
from linopy_yaml.where_parser import (
    AndNode,
    BoolLiteral,
    Comparison,
    DimCmp,
    DimDefined,
    ExistenceCheck,
    NotNode,
    OrNode,
    ParamCmp,
    ParamDefined,
    WhereNode,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from linopy_yaml.schema import MathSchema

#: helper kwargs whose *value* names a dimension — ``sum(x, over=generator)``.
#: The key form (``roll(x, snapshot=1)``) is handled separately: the dimension
#: is the key, so there is no value node to resolve.
_DIM_VALUE_KWARGS: dict[str, tuple[str, ...]] = {'sum': ('over',), 'group_sum': ('into',)}


class Namespace:
    """The declared names of one schema, by kind.

    Flat by construction: :meth:`kind` is a single lookup, not an ordered
    walk through several stores.
    """

    __slots__ = ('dimensions', 'parameters', 'variables')

    def __init__(
        self,
        variables: Iterable[str],
        parameters: Iterable[str],
        dimensions: Iterable[str],
    ) -> None:
        self.variables = frozenset(variables)
        self.parameters = frozenset(parameters)
        self.dimensions = frozenset(dimensions)

    @classmethod
    def of(cls, schema: MathSchema, known_variables: Iterable[str] = ()) -> Namespace:
        """Build the namespace of *schema*.

        ``known_variables`` widens the variable set only — used by
        ``compat.extend()``, where expressions may reference variables already
        on the model. Parameters get no such widening: a YAML file declares
        every parameter it uses (hard rule 5).
        """
        return cls(
            set(schema.variables) | set(known_variables),
            schema.parameters,
            schema.dimensions,
        )

    def kind(self, name: str) -> str | None:
        """``'variable'`` | ``'parameter'`` | ``'dimension'`` | ``None``."""
        if name in self.variables:
            return 'variable'
        if name in self.parameters:
            return 'parameter'
        if name in self.dimensions:
            return 'dimension'
        return None

    def _unknown(self, name: str, context: str, *, allow_dims: bool) -> str:
        kinds = ['Variables', 'Parameters'] if not allow_dims else ['Parameters', 'Dimensions']
        values = (
            [sorted(self.variables), sorted(self.parameters)]
            if not allow_dims
            else [sorted(self.parameters), sorted(self.dimensions)]
        )
        listing = '\n'.join(f'  {k}: {v}' for k, v in zip(kinds, values, strict=True))
        return f"{context}: '{name}' not found.\n{listing}\nCheck for typos, or ensure '{name}' is declared."


# ---------------------------------------------------------------------------
# the seam both backends use
# ---------------------------------------------------------------------------


def expression_of(text: str, schema: MathSchema, ns: Namespace, context: str) -> ExprNode:
    """Parse, expand and resolve *text* — the only way a backend gets an AST.

    Raises :class:`LanguageError` listing every problem. ``validation.py`` calls the
    same path at load time, so by the time a backend calls this the result is
    known to be clean; calling it again is how the backend gets a *typed* tree
    without duplicating the pass.
    """
    from linopy_yaml.expansion import parse_and_expand
    from linopy_yaml.relational.executor import RelationalBuildError

    errors: list[str] = []
    resolved = resolve_expression(parse_and_expand(text, schema, context), ns, context, errors)
    if errors:
        raise RelationalBuildError('\n'.join(errors))
    assert resolved is not None
    return resolved


def where_of(text: str | None, ns: Namespace, context: str) -> WhereNode | None:
    """Parse and resolve a where string; ``None`` stays ``None``."""
    from linopy_yaml.relational.executor import RelationalBuildError
    from linopy_yaml.where_parser import parse_where

    if text is None:
        return None
    errors: list[str] = []
    resolved = resolve_where(parse_where(text), ns, context, errors)
    if errors:
        raise RelationalBuildError('\n'.join(errors))
    return resolved


# ---------------------------------------------------------------------------
# expressions
# ---------------------------------------------------------------------------


def resolve_expression(
    node: ExprNode,
    ns: Namespace,
    context: str,
    errors: list[str],
) -> ExprNode | None:
    """Rewrite every ``NameNode`` under *node* to a typed node.

    Appends to *errors* and returns ``None`` if anything failed to resolve, so
    a caller collecting problems across a whole schema reports them together.
    """
    before = len(errors)
    if isinstance(node, CompareNode):
        resolved: ExprNode = CompareNode(
            node.op,
            _resolve_arith(node.left, ns, context, errors),
            _resolve_arith(node.right, ns, context, errors),
        )
    else:
        resolved = _resolve_arith(node, ns, context, errors)
    return None if len(errors) > before else resolved


def _resolve_arith(node: ArithNode, ns: Namespace, context: str, errors: list[str]) -> ArithNode:
    if isinstance(node, NumberNode):
        return node

    if isinstance(node, (VarNode, ParamNode, DimRefNode)):
        return node  # already resolved (idempotent — piecewise re-resolves)

    if isinstance(node, NameNode):
        match ns.kind(node.name):
            case 'variable':
                return VarNode(node.name)
            case 'parameter':
                return ParamNode(node.name)
            case 'dimension':
                errors.append(
                    f"{context}: '{node.name}' is a dimension, and a dimension is "
                    f'not a value in an expression. Dimensions appear in '
                    f"'foreach:', in helper arguments (sum(x, over={node.name})), "
                    f'and in where-comparisons — to use its coordinates as data, '
                    f'declare a parameter over it.'
                )
                return node
            case _:
                errors.append(ns._unknown(node.name, context, allow_dims=False))
                return node

    if isinstance(node, UnaryOpNode):
        return UnaryOpNode(node.op, _resolve_arith(node.operand, ns, context, errors))

    if isinstance(node, BinOpNode):
        return BinOpNode(
            node.op,
            _resolve_arith(node.left, ns, context, errors),
            _resolve_arith(node.right, ns, context, errors),
        )

    if isinstance(node, FuncCallNode):
        if node.name not in BUILTIN_NAMES:
            from linopy_yaml.helpers import unknown_helper_message

            errors.append(f'{context}: {unknown_helper_message(node.name)}')
            return node
        args = [_resolve_arith(a, ns, context, errors) for a in node.args]
        kwargs: dict[str, ArithNode] = {}
        dim_valued = _DIM_VALUE_KWARGS.get(node.name, ())
        for key, value in node.kwargs.items():
            # roll(x, snapshot=1) puts the dimension in the *key*, so there is
            # no value node to type — check the key names a declared dim
            if node.name in _DIM_KEY_HELPERS and key not in ns.dimensions:
                errors.append(_undeclared_dim(context, node.name, f'{key}=...', key, ns))
            kwargs[key] = (
                _resolve_dim_ref(value, ns, context, node.name, key, errors)
                if key in dim_valued
                else _resolve_arith(value, ns, context, errors)
            )
        return FuncCallNode(node.name, args, kwargs)

    assert_never(node)


#: helpers whose keyword *key* names a dimension — ``roll(x, snapshot=1)``
_DIM_KEY_HELPERS = frozenset({'roll', 'shift'})


def _undeclared_dim(context: str, helper: str, shown: str, name: str, ns: Namespace) -> str:
    return (
        f'{context}: {helper}({shown}) does not name a declared dimension.\n'
        f'  Dimensions: {sorted(ns.dimensions)}\n'
        f"Declare '{name}' under 'dimensions:', or fix the typo — an unknown "
        f'dimension makes {helper}() a silent no-op rather than an error.'
    )


def _resolve_dim_ref(
    value: ArithNode,
    ns: Namespace,
    context: str,
    helper: str,
    key: str,
    errors: list[str],
) -> ArithNode:
    """Resolve a helper kwarg whose *value* must name a declared dimension."""
    if isinstance(value, DimRefNode):
        return value
    if not isinstance(value, NameNode):
        errors.append(f'{context}: {helper}({key}=...) must name a dimension.')
        return value
    if value.name not in ns.dimensions:
        errors.append(_undeclared_dim(context, helper, f'{key}={value.name}', value.name, ns))
        return value
    return DimRefNode(value.name)


# ---------------------------------------------------------------------------
# where strings
# ---------------------------------------------------------------------------


def resolve_where(
    node: WhereNode,
    ns: Namespace,
    context: str,
    errors: list[str],
) -> WhereNode | None:
    """Rewrite a parsed where AST into typed predicates.

    Both parameters and dimensions are legal here — a where-string is a
    predicate over the frame, and the frame carries its own coordinates. What
    is *not* legal is an unknown name: it used to mean "scalar False" in the
    eager lane, which silently produced an empty model.
    """
    before = len(errors)
    resolved = _resolve_where(node, ns, context, errors)
    return None if len(errors) > before else resolved


def _resolve_where(node: WhereNode, ns: Namespace, context: str, errors: list[str]) -> WhereNode:
    if isinstance(node, BoolLiteral):
        return node

    if isinstance(node, (ParamCmp, DimCmp, ParamDefined, DimDefined)):
        return node  # already resolved

    if isinstance(node, ExistenceCheck):
        match ns.kind(node.name):
            case 'parameter':
                return ParamDefined(node.name)
            case 'dimension':
                return DimDefined(node.name)
            case 'variable':
                errors.append(
                    f"{context}: where references variable '{node.name}'. A where "
                    f'mask is built before variables exist — it may test parameters '
                    f'and dimension coordinates only.'
                )
                return node
            case _:
                errors.append(ns._unknown(node.name, context, allow_dims=True))
                return node

    if isinstance(node, Comparison):
        value = node.value
        # A bare-name right-hand side is ambiguous by construction: the where
        # grammar has no string quoting. Resolve it the same way as any other
        # name, so the meaning cannot depend on which parameters happen to be
        # declared.
        if isinstance(value, str) and ns.kind(value) == 'parameter':
            errors.append(
                f"{context}: '{node.name} {node.op} {value}' compares two "
                f'parameters, which is not in the language — a where-comparison '
                f'tests one parameter or dimension against a literal. Precompute '
                f'the comparison as a boolean parameter in data prep and test that.'
            )
            return node
        if isinstance(value, str) and ns.kind(value) == 'variable':
            errors.append(
                f"{context}: '{node.name} {node.op} {value}' compares against "
                f'variable {value!r}. A where mask is built before variables exist.'
            )
            return node

        match ns.kind(node.name):
            case 'parameter':
                return ParamCmp(node.name, node.op, value)
            case 'dimension':
                return DimCmp(node.name, node.op, value)
            case 'variable':
                errors.append(
                    f"{context}: where references variable '{node.name}'. A where "
                    f'mask is built before variables exist — it may test parameters '
                    f'and dimension coordinates only.'
                )
                return node
            case _:
                errors.append(ns._unknown(node.name, context, allow_dims=True))
                return node

    if isinstance(node, NotNode):
        return NotNode(_resolve_where(node.operand, ns, context, errors))
    if isinstance(node, AndNode):
        return AndNode(
            _resolve_where(node.left, ns, context, errors),
            _resolve_where(node.right, ns, context, errors),
        )
    if isinstance(node, OrNode):
        return OrNode(
            _resolve_where(node.left, ns, context, errors),
            _resolve_where(node.right, ns, context, errors),
        )

    assert_never(node)
