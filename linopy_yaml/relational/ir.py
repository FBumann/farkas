"""Logical-plan IR for relational LP construction (SPEC.md §12.4).

Frozen dataclasses only — no execution logic, no engine imports. A `Program`
is a complete declarative description of a linear program over named tidy
tables; actual data is bound at execution time via a source registry.

Expressions support operator sugar so plans read naturally in Python:

    balance = GroupSum(Var("p"), mapping="gen_bus", into="bus") - Param("load")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Sense = Literal['==', '<=', '>=']
ObjSense = Literal['min', 'max']
CmpOp = Literal['==', '!=', '<=', '>=', '<', '>']
VType = Literal['continuous', 'binary', 'integer']


# --------------------------------------------------------------------------
# Affine expressions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Expr:
    """Base class for affine expressions over variables and parameters."""

    def __add__(self, other: Expr | float | int) -> Expr:
        return Add(self, _coerce(other))

    def __radd__(self, other: Expr | float | int) -> Expr:
        return Add(_coerce(other), self)

    def __sub__(self, other: Expr | float | int) -> Expr:
        return Add(self, Neg(_coerce(other)))

    def __rsub__(self, other: Expr | float | int) -> Expr:
        return Add(_coerce(other), Neg(self))

    def __mul__(self, other: Expr | float | int) -> Expr:
        return Mul(self, _coerce(other))

    def __rmul__(self, other: Expr | float | int) -> Expr:
        return Mul(_coerce(other), self)

    def __truediv__(self, other: Expr | float | int) -> Expr:
        return Div(self, _coerce(other))

    def __neg__(self) -> Expr:
        return Neg(self)


def _coerce(x: Expr | float | int) -> Expr:
    if isinstance(x, Expr):
        return x
    return Const(float(x))


@dataclass(frozen=True)
class Const(Expr):
    """A scalar constant."""

    value: float


@dataclass(frozen=True)
class Param(Expr):
    """A parameter reference — contributes to the constant part."""

    name: str


@dataclass(frozen=True)
class Var(Expr):
    """A variable reference — one term per existing variable row."""

    name: str


@dataclass(frozen=True)
class Neg(Expr):
    x: Expr


@dataclass(frozen=True)
class Add(Expr):
    a: Expr
    b: Expr


@dataclass(frozen=True)
class Mul(Expr):
    """Product. At least one factor must be variable-free (affine algebra)."""

    a: Expr
    b: Expr


@dataclass(frozen=True)
class Div(Expr):
    """Quotient ``a / b``. The divisor must be variable-free."""

    a: Expr
    b: Expr


@dataclass(frozen=True)
class Sum(Expr):
    """Sum ``x`` over the named dims, removing them from the result."""

    x: Expr
    over: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.over, str):  # tolerate Sum(x, "generator")
            object.__setattr__(self, 'over', (self.over,))


@dataclass(frozen=True)
class GroupSum(Expr):
    """Sum ``x`` through a mapping parameter.

    ``mapping`` names a parameter with exactly one dim ``d`` whose value
    column holds group keys; the result replaces dim ``d`` with dim ``into``.
    """

    x: Expr
    mapping: str
    into: str


@dataclass(frozen=True)
class Shift(Expr):
    """Shift along ``dim``: the result at coord *t* is ``x`` at coord *t-n*.

    ``wrap=True`` is periodic (matching ``xarray.roll``); ``wrap=False`` is
    acyclic — positions shifted past the edge contribute zero (row absence).
    """

    x: Expr
    dim: str
    n: int
    wrap: bool = True


# --------------------------------------------------------------------------
# Predicates (where masks — row absence)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Pred:
    """Base class for where-predicates."""


@dataclass(frozen=True)
class Cmp(Pred):
    param: str
    op: CmpOp
    value: float | str


@dataclass(frozen=True)
class DimCmp(Pred):
    """Compare a *dimension coordinate* to a literal — ``where: "snapshot > 0"``.

    Unlike :class:`Cmp`, no parameter is involved: the dim table is already in
    the frame, so this is a filter on its own column.
    """

    dim: str
    op: CmpOp
    value: float | str


@dataclass(frozen=True)
class Defined(Pred):
    """True where the parameter has a non-null, finite value."""

    param: str


@dataclass(frozen=True)
class Bool(Pred):
    """Constant predicate (``Bool(False)`` masks out every row)."""

    value: bool


@dataclass(frozen=True)
class And(Pred):
    a: Pred
    b: Pred


@dataclass(frozen=True)
class Or(Pred):
    a: Pred
    b: Pred


@dataclass(frozen=True)
class Not(Pred):
    x: Pred


# --------------------------------------------------------------------------
# Declarations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParameterDecl:
    """Shape declaration; data is bound at execution time by name."""

    name: str
    dims: tuple[str, ...]


@dataclass(frozen=True)
class VariableDecl:
    name: str
    dims: tuple[str, ...]
    where: Pred | None = None
    lower: Expr = field(default_factory=lambda: Const(float('-inf')))
    upper: Expr = field(default_factory=lambda: Const(float('inf')))
    vtype: VType = 'continuous'


@dataclass(frozen=True)
class ConstraintDecl:
    """``lhs sense rhs`` for each coord combination of ``dims``.

    Both sides are affine; the executor normalises constants to the RHS.
    ``where`` masks out coord combinations (row absence, like variables).
    """

    name: str
    dims: tuple[str, ...]
    lhs: Expr
    sense: Sense
    rhs: Expr
    where: Pred | None = None


@dataclass(frozen=True)
class ObjectiveDecl:
    """Objective; dims remaining after explicit Sums are implicitly summed."""

    sense: ObjSense
    expr: Expr


@dataclass(frozen=True)
class Program:
    """A complete linear program over named tidy tables."""

    parameters: tuple[ParameterDecl, ...]
    variables: tuple[VariableDecl, ...]
    constraints: tuple[ConstraintDecl, ...]
    objective: ObjectiveDecl

    def parameter(self, name: str) -> ParameterDecl:
        for p in self.parameters:
            if p.name == name:
                return p
        raise KeyError(f"unknown parameter '{name}'")

    def variable(self, name: str) -> VariableDecl:
        for v in self.variables:
            if v.name == name:
                return v
        raise KeyError(f"unknown variable '{name}'")
