"""The logical plan: relational LP construction, one step above SQL.

An intermediate representation in the compiler sense — the module is named
for what it *is* to this engine (duckdb, Calcite and Spark all call this
shape a logical plan) rather than for the generic category.

The lane is described in docs/ARCHITECTURE.md, "The relational lane".

Frozen dataclasses only — no execution logic, no engine imports. A `Program`
is a complete declarative description of a linear program over named tidy
tables; actual data is bound at execution time via a source registry.

Expressions support operator sugar so plans read naturally in Python:

    balance = GroupSum(Variable("p"), over="generator", coordinate="bus", into="bus") - Parameter("load")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeVar

from lpspec.errors import unknown_name_message

ConstraintSense = Literal['==', '<=', '>=']
ObjectiveSense = Literal['min', 'max']
ComparisonOperator = Literal['==', '!=', '<=', '>=', '<', '>']
VariableType = Literal['continuous', 'binary', 'integer']


# --------------------------------------------------------------------------
# Affine expressions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Expression:
    """Base class for affine expressions over variables and parameters."""

    def __add__(self, other: Expression | float | int) -> Expression:
        return Add(self, _coerce(other))

    def __radd__(self, other: Expression | float | int) -> Expression:
        return Add(_coerce(other), self)

    def __sub__(self, other: Expression | float | int) -> Expression:
        return Add(self, Negate(_coerce(other)))

    def __rsub__(self, other: Expression | float | int) -> Expression:
        return Add(_coerce(other), Negate(self))

    def __mul__(self, other: Expression | float | int) -> Expression:
        return Multiply(self, _coerce(other))

    def __rmul__(self, other: Expression | float | int) -> Expression:
        return Multiply(_coerce(other), self)

    def __truediv__(self, other: Expression | float | int) -> Expression:
        return Divide(self, _coerce(other))

    def __neg__(self) -> Expression:
        return Negate(self)


def _coerce(x: Expression | float | int) -> Expression:
    if isinstance(x, Expression):
        return x
    return Constant(float(x))


@dataclass(frozen=True)
class Constant(Expression):
    """A scalar constant."""

    value: float


@dataclass(frozen=True)
class Parameter(Expression):
    """A parameter reference — contributes to the constant part."""

    name: str


@dataclass(frozen=True)
class Variable(Expression):
    """A variable reference — one term per existing variable row."""

    name: str


@dataclass(frozen=True)
class Negate(Expression):
    operand: Expression


@dataclass(frozen=True)
class Add(Expression):
    left: Expression
    right: Expression


@dataclass(frozen=True)
class Multiply(Expression):
    """Product. At least one factor must be variable-free (affine algebra)."""

    left: Expression
    right: Expression


@dataclass(frozen=True)
class Divide(Expression):
    """Quotient ``numerator / divisor``. The divisor must be variable-free."""

    numerator: Expression
    divisor: Expression


@dataclass(frozen=True)
class Sum(Expression):
    """Sum ``operand`` over the named dims, removing them from the result."""

    operand: Expression
    over: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.over, str):  # tolerate Sum(operand, "generator")
            object.__setattr__(self, 'over', (self.over,))


@dataclass(frozen=True)
class GroupSum(Expression):
    """Sum ``operand`` through a coordinate declared on dim ``over``.

    ``coordinate`` names a coordinate carried by dim ``over`` whose values are
    labels of dim ``into``; the result replaces ``over`` with ``into``. All
    three are resolved before lowering, so the executor needs no schema lookup
    to place the terms.
    """

    operand: Expression
    over: str
    coordinate: str
    into: str


@dataclass(frozen=True)
class Translate(Expression):
    """Re-index along one dimension: the result at coord *t* is ``operand`` at
    coord *t - by*.

    One node for both surface spellings, which differ only in ``wrap``:
    ``roll`` is ``wrap=True`` (periodic, matching ``xarray.roll``), ``shift``
    is ``wrap=False``. The node is named for the coordinate map rather than for
    either spelling, so it does not read as one of the two.

    ``fill`` decides what an acyclic shift leaves behind. ``None`` — the
    default and what bare ``shift`` lowers to — means the vacated positions are
    **absent**, so they propagate and drop the row, which is what linopy v1
    means by ``.shift()``. A number means they are present and contribute it,
    which is the ``.fillna(0)`` escape hatch spelled in the language. It is
    always ``None`` when ``wrap`` is true, since a cyclic map vacates nothing.
    """

    operand: Expression
    dimension: str
    by: int
    wrap: bool = True
    fill: float | None = None


def children(expression: Expression) -> tuple[Expression, ...]:
    """The sub-expressions of *expression* — the structural half of any walk.

    Three passes recurse over this tree (degree in ``lowering._has_var``, and
    both halves of :func:`divisor_parameters`), and they differ only in what
    they do at the leaves. Enumerating the children once per pass is how a node
    added later reaches two of them and not the third.
    """
    if isinstance(expression, Negate):
        return (expression.operand,)
    if isinstance(expression, (Add, Multiply)):
        return (expression.left, expression.right)
    if isinstance(expression, Divide):
        return (expression.numerator, expression.divisor)
    if isinstance(expression, (Sum, GroupSum, Translate)):
        return (expression.operand,)
    return ()


# --------------------------------------------------------------------------
# Predicates (where masks — row absence)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Predicate:
    """Base class for where-predicates."""


@dataclass(frozen=True)
class ParameterComparison(Predicate):
    parameter: str
    op: ComparisonOperator
    value: float | str


@dataclass(frozen=True)
class DimensionComparison(Predicate):
    """Compare a *dimension coordinate* to a literal — ``where: "snapshot > 0"``.

    Unlike :class:`ParameterComparison`, no parameter is involved: the dim
    table is already in the frame, so this is a filter on its own column.
    """

    dimension: str
    op: ComparisonOperator
    value: float | str


@dataclass(frozen=True)
class ParameterDefined(Predicate):
    """True where the parameter has a non-null, finite value."""

    parameter: str


@dataclass(frozen=True)
class VariableDefined(Predicate):
    """True at the coordinates where the variable exists.

    A semi-join against the variable's own frame — pointwise, and the same
    shape as any mapping-table join.
    """

    variable: str


@dataclass(frozen=True)
class BooleanConstant(Predicate):
    """Constant predicate (``BooleanConstant(False)`` masks out every row)."""

    value: bool


@dataclass(frozen=True)
class And(Predicate):
    left: Predicate
    right: Predicate


@dataclass(frozen=True)
class Or(Predicate):
    left: Predicate
    right: Predicate


@dataclass(frozen=True)
class Not(Predicate):
    operand: Predicate


# --------------------------------------------------------------------------
# Declarations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DimensionDeclaration:
    """A dimension and the coordinates its labels carry.

    ``coordinates`` maps a coordinate name to the dimension its values are
    labels of. The executor checks that containment once the dim tables exist,
    which is what keeps a mistyped label from silently dropping its terms in
    the inner join that places them.
    """

    name: str
    coordinates: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ParameterDeclaration:
    """Shape declaration; data is bound at execution time by name."""

    name: str
    dims: tuple[str, ...]


@dataclass(frozen=True)
class VariableDeclaration:
    name: str
    dims: tuple[str, ...]
    where: Predicate | None = None
    lower: Expression = field(default_factory=lambda: Constant(float('-inf')))
    upper: Expression = field(default_factory=lambda: Constant(float('inf')))
    variable_type: VariableType = 'continuous'


@dataclass(frozen=True)
class ConstraintDeclaration:
    """``lhs sense rhs`` for each coord combination of ``dims``.

    Both sides are affine; the executor normalises constants to the RHS.
    ``where`` masks out coord combinations (row absence, like variables).
    """

    name: str
    dims: tuple[str, ...]
    lhs: Expression
    sense: ConstraintSense
    rhs: Expression
    where: Predicate | None = None


@dataclass(frozen=True)
class ObjectiveDeclaration:
    """Objective; dims remaining after explicit Sums are implicitly summed."""

    sense: ObjectiveSense
    expression: Expression


_Declaration = TypeVar('_Declaration', ParameterDeclaration, VariableDeclaration, ConstraintDeclaration)


def _declared(items: tuple[_Declaration, ...], name: str, kind: str) -> _Declaration:
    """The declaration called *name*, or a ``KeyError`` naming the near miss."""
    for item in items:
        if item.name == name:
            return item
    raise KeyError(unknown_name_message(kind, name, (i.name for i in items)))


@dataclass(frozen=True)
class Program:
    """A complete linear program over named tidy tables."""

    parameters: tuple[ParameterDeclaration, ...]
    variables: tuple[VariableDeclaration, ...]
    constraints: tuple[ConstraintDeclaration, ...]
    objective: ObjectiveDeclaration
    dimensions: tuple[DimensionDeclaration, ...] = ()

    def dimension(self, name: str) -> DimensionDeclaration:
        """The dimension called *name*. Undeclared is not an error here: a
        dimension with no coordinates has nothing to declare."""
        for d in self.dimensions:
            if d.name == name:
                return d
        return DimensionDeclaration(name)

    def parameter(self, name: str) -> ParameterDeclaration:
        return _declared(self.parameters, name, 'parameter')

    def variable(self, name: str) -> VariableDeclaration:
        return _declared(self.variables, name, 'variable')

    def constraint(self, name: str) -> ConstraintDeclaration:
        return _declared(self.constraints, name, 'constraint')


def divisor_parameters(*expressions: Expression) -> frozenset[str]:
    """Parameters appearing anywhere in a divisor position.

    Static, like every other question this module answers: which names *can*
    reach a divisor is decided by the plan, and *where* they must have values is
    decided by the rows a declaration actually builds. Splitting it that way
    keeps the coverage check off parameters that can never need it, and off the
    coordinates a ``where`` already removed.
    """

    found: set[str] = set()

    def names(e: Expression) -> None:
        """Every parameter under *e*, wherever it sits."""
        if isinstance(e, Parameter):
            found.add(e.name)
        for child in children(e):
            names(child)

    def walk(e: Expression) -> None:
        """Every divisor under *e*, whose parameters are the answer."""
        if isinstance(e, Divide):
            names(e.divisor)
        for child in children(e):
            walk(child)

    for e in expressions:
        walk(e)
    return frozenset(found)
