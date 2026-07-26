"""Name resolution — the pass that makes the core AST fully typed.

Parsers emit ``NameNode``: a token, not yet a meaning. This module rewrites
each one into a typed node (``VariableNode`` / ``ParameterNode`` / ``DimensionNode`` /
``CoordinateNode``, and
``ParameterComparisonNode`` / ``DimensionComparisonNode`` / ``ParameterDefinedNode`` on the where
side), so the AST reaching either backend holds no unresolved names.

Doing this once, here, is what makes scoping identical across the lanes by
construction rather than by test. When each backend resolved for itself they
disagreed three ways, every one of which built a model on one lane and raised
on the other — see SPEC §5.3 for the list and the rules that replace it.

The namespace is flat and collisions are load errors (``schema.py``); macro
formals are the one scope, and may not collide with a declared dimension.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from linopy_yaml.errors import LanguageError
from linopy_yaml.expansion import parse_and_expand
from linopy_yaml.expression_parser import (
    ArithmeticNode,
    BinaryOperatorNode,
    ComparisonNode,
    CoordinateNode,
    DimensionNode,
    ExpressionNode,
    FunctionCallNode,
    NameNode,
    NumberNode,
    ParameterNode,
    UnaryOperatorNode,
    VariableNode,
)
from linopy_yaml.helpers import BUILTINS, call_shape_error, unknown_helper_message
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
    parse_where,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from linopy_yaml.schema import MathSchema


class Namespace:
    """The declared names of one schema, by kind.

    Flat by construction: :meth:`kind` is a single lookup, not an ordered
    walk through several stores.
    """

    __slots__ = ('coordinates', 'dimensions', 'parameters', 'variables')

    def __init__(
        self,
        variables: Iterable[str],
        parameters: Iterable[str],
        dimensions: Iterable[str],
        coordinates: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self.variables = frozenset(variables)
        self.parameters = frozenset(parameters)
        self.dimensions = frozenset(dimensions)
        #: dim -> {coordinate name: target dim}. Scoped, so it is not part of
        #: :meth:`kind` — a coordinate name is only meaningful under its dim.
        self.coordinates: dict[str, dict[str, str]] = {d: dict(c) for d, c in (coordinates or {}).items()}

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
            {d: dd.coords for d, dd in schema.dimensions.items()},
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


def expression_of(text: str, schema: MathSchema, ns: Namespace, context: str) -> ExpressionNode:
    """Parse, expand and resolve *text* — the only way a backend gets an AST.

    Raises :class:`LanguageError` listing every problem. ``validation.py`` calls the
    same path at load time, so by the time a backend calls this the result is
    known to be clean; calling it again is how the backend gets a *typed* tree
    without duplicating the pass.
    """
    errors: list[str] = []
    resolved = resolve_expression(parse_and_expand(text, schema, context), ns, context, errors)
    if errors:
        raise LanguageError('\n'.join(errors))
    assert resolved is not None
    return resolved


def where_of(text: str | None, ns: Namespace, context: str) -> WhereNode | None:
    """Parse and resolve a where string; ``None`` stays ``None``."""
    if text is None:
        return None
    errors: list[str] = []
    resolved = resolve_where(parse_where(text), ns, context, errors)
    if errors:
        raise LanguageError('\n'.join(errors))
    return resolved


# ---------------------------------------------------------------------------
# expressions
# ---------------------------------------------------------------------------


def resolve_expression(
    node: ExpressionNode,
    ns: Namespace,
    context: str,
    errors: list[str],
) -> ExpressionNode | None:
    """Rewrite every ``NameNode`` under *node* to a typed node.

    Appends to *errors* and returns ``None`` if anything failed to resolve, so
    a caller collecting problems across a whole schema reports them together.

    Helper *call shapes* are checked here too (``helpers.call_shape_error``).
    Arity is a language rule, and this is the pass every consumer goes through,
    so neither backend has to state a signature a second time.
    """
    before = len(errors)
    if isinstance(node, ComparisonNode):
        resolved: ExpressionNode = ComparisonNode(
            node.op,
            _resolve_arith(node.left, ns, context, errors),
            _resolve_arith(node.right, ns, context, errors),
        )
    else:
        resolved = _resolve_arith(node, ns, context, errors)
    return None if len(errors) > before else resolved


def _resolve_arith(node: ArithmeticNode, ns: Namespace, context: str, errors: list[str]) -> ArithmeticNode:
    if isinstance(node, NumberNode):
        return node

    if isinstance(node, (VariableNode, ParameterNode, DimensionNode, CoordinateNode)):
        return node  # idempotent: piecewise re-resolves expanded links

    if isinstance(node, NameNode):
        match ns.kind(node.name):
            case 'variable':
                return VariableNode(node.name)
            case 'parameter':
                return ParameterNode(node.name)
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

    if isinstance(node, UnaryOperatorNode):
        return UnaryOperatorNode(node.op, _resolve_arith(node.operand, ns, context, errors))

    if isinstance(node, BinaryOperatorNode):
        return BinaryOperatorNode(
            node.op,
            _resolve_arith(node.left, ns, context, errors),
            _resolve_arith(node.right, ns, context, errors),
        )

    if isinstance(node, FunctionCallNode):
        if node.name not in BUILTINS:
            errors.append(f'{context}: {unknown_helper_message(node.name)}')
            return node
        builtin = BUILTINS[node.name]
        shape_error = call_shape_error(node.name, len(node.args), node.kwargs)
        if shape_error is not None:
            errors.append(f'{context}: {shape_error}')
        args = [_resolve_arith(a, ns, context, errors) for a in node.args]
        kwargs: dict[str, ArithmeticNode] = {}
        for key, value in node.kwargs.items():
            # roll(x, snapshot=1): the dim is the key, so there is no node to type
            if builtin.dimension_is_key and key not in ns.dimensions:
                errors.append(_undeclared_dim(context, node.name, f'{key}=...', key, ns))
            if key in builtin.dimension_kwargs:
                kwargs[key] = _resolve_dim_ref(value, ns, context, node.name, key, errors)
            elif key in builtin.coordinate_kwargs:
                # scoped to the sibling over= dim, so that kwarg has to be read
                # here rather than resolved on its own
                kwargs[key] = _resolve_coordinate_ref(
                    value, node.kwargs.get('over'), ns, context, node.name, key, errors
                )
            else:
                kwargs[key] = _resolve_arith(value, ns, context, errors)
        return FunctionCallNode(node.name, args, kwargs)

    assert_never(node)


def _undeclared_dim(context: str, helper: str, shown: str, name: str, ns: Namespace) -> str:
    return (
        f'{context}: {helper}({shown}) does not name a declared dimension.\n'
        f'  Dimensions: {sorted(ns.dimensions)}\n'
        f"Declare '{name}' under 'dimensions:', or fix the typo — an unknown "
        f'dimension makes {helper}() a silent no-op rather than an error.'
    )


def _resolve_dim_ref(
    value: ArithmeticNode,
    ns: Namespace,
    context: str,
    helper: str,
    key: str,
    errors: list[str],
) -> ArithmeticNode:
    """Resolve a helper kwarg whose *value* must name a declared dimension."""
    if isinstance(value, DimensionNode):
        return value
    if not isinstance(value, NameNode):
        errors.append(f'{context}: {helper}({key}=...) must name a dimension.')
        return value
    if value.name not in ns.dimensions:
        errors.append(_undeclared_dim(context, helper, f'{key}={value.name}', value.name, ns))
        return value
    return DimensionNode(value.name)


def _resolve_coordinate_ref(
    value: ArithmeticNode,
    over: ArithmeticNode | None,
    ns: Namespace,
    context: str,
    helper: str,
    key: str,
    errors: list[str],
) -> ArithmeticNode:
    """Resolve a helper kwarg naming a coordinate on the sibling ``over=`` dim."""
    if isinstance(value, CoordinateNode):
        return value
    if not isinstance(value, (NameNode, DimensionNode)):
        errors.append(f'{context}: {helper}({key}=...) must name a coordinate.')
        return value
    if not isinstance(over, (NameNode, DimensionNode)):
        errors.append(
            f'{context}: {helper}({key}={value.name}) needs a sibling over=<dim> '
            f'naming the dimension that carries the coordinate.'
        )
        return value
    declared = ns.coordinates.get(over.name, {})
    if value.name not in declared:
        listing = (
            f'  Coordinates on {over.name}: {sorted(declared)}'
            if declared
            else f"  '{over.name}' declares no coordinates."
        )
        errors.append(
            f'{context}: {helper}(over={over.name}, {key}={value.name}) does not name a '
            f"coordinate of '{over.name}'.\n{listing}\n"
            f"Declare it under 'dimensions.{over.name}.coords', naming the dimension "
            f'its values are labels of.'
        )
        return value
    return CoordinateNode(value.name, dimension=over.name, into=declared[value.name])


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
    if isinstance(node, BooleanLiteralNode):
        return node

    if isinstance(node, (ParameterComparisonNode, DimensionComparisonNode, ParameterDefinedNode)):
        return node  # already resolved

    if isinstance(node, UnresolvedNameNode):
        match ns.kind(node.name):
            case 'parameter':
                return ParameterDefinedNode(node.name)
            case 'dimension':
                errors.append(
                    f"{context}: '{node.name}' is a dimension, and a bare dimension "
                    f'name is true at every coordinate — the mask has no effect. '
                    f'Remove it, or compare it: where: "{node.name} > 0".'
                )
                return node
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

    if isinstance(node, UnresolvedComparisonNode):
        value = node.value
        # The grammar has no string quoting, so a bare-name RHS is ambiguous;
        # resolving it like any other name keeps the meaning declaration-independent.
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
                return ParameterComparisonNode(node.name, node.op, value)
            case 'dimension':
                return DimensionComparisonNode(node.name, node.op, value)
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
