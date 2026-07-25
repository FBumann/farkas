"""Pydantic models for YAML schema validation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, field_validator, model_validator

from linopy_yaml.helpers import BUILTIN_NAMES

if TYPE_CHECKING:
    from collections.abc import Iterable


class DimensionDef(BaseModel):
    """A declared dimension with optional dtype and values."""

    dtype: str = 'str'
    values: list[Any] | None = None

    @field_validator('dtype')
    @classmethod
    def _check_dtype(cls, v: str) -> str:
        allowed = {'float', 'int', 'str', 'datetime'}
        if v not in allowed:
            msg = f"dtype must be one of {allowed}, got '{v}'"
            raise ValueError(msg)
        return v


class ParameterDef(BaseModel):
    """A declared parameter with dims and dtype."""

    dims: list[str]
    dtype: str = 'float'

    @field_validator('dtype')
    @classmethod
    def _check_dtype(cls, v: str) -> str:
        allowed = {'float', 'int', 'bool', 'str'}
        if v not in allowed:
            msg = f"dtype must be one of {allowed}, got '{v}'"
            raise ValueError(msg)
        return v


class BoundsDef(BaseModel):
    """Variable bounds — each side is a number or parameter name.

    The defaults are linopy's (``add_variables(lower=-inf, upper=inf)``): a
    declaration that omits a bound means the variable is unbounded on that
    side, not implicitly non-negative. Non-negativity is a real constraint,
    so the file has to say it.
    """

    lower: float | str = float('-inf')
    upper: float | str = float('inf')


class VariableDef(BaseModel):
    """A declared decision variable."""

    foreach: list[str]
    where: str | None = None
    bounds: BoundsDef = BoundsDef()
    binary: bool = False
    integer: bool = False

    @model_validator(mode='after')
    def _check_binary_integer(self) -> VariableDef:
        if self.binary and self.integer:
            msg = 'A variable cannot be both binary and integer.'
            raise ValueError(msg)
        return self


class EquationDef(BaseModel):
    """A single equation inside a constraint or objective."""

    expression: str
    where: str | None = None


class ConstraintDef(BaseModel):
    """A declared constraint with foreach, where, and equations."""

    foreach: list[str]
    where: str | None = None
    equations: list[EquationDef]

    @field_validator('equations')
    @classmethod
    def _at_least_one(cls, v: list[EquationDef]) -> list[EquationDef]:
        if not v:
            msg = 'A constraint must have at least one equation.'
            raise ValueError(msg)
        return v


class ObjectiveDef(BaseModel):
    """A declared objective function."""

    sense: str = 'minimize'
    equations: list[EquationDef]

    @field_validator('sense')
    @classmethod
    def _check_sense(cls, v: str) -> str:
        allowed = {'minimize', 'maximize'}
        if v not in allowed:
            msg = f"sense must be one of {allowed}, got '{v}'"
            raise ValueError(msg)
        return v

    @field_validator('equations')
    @classmethod
    def _at_least_one(cls, v: list[EquationDef]) -> list[EquationDef]:
        if not v:
            msg = 'An objective must have at least one equation.'
            raise ValueError(msg)
        return v


class MacroDef(BaseModel):
    """A parameterised expression template, defined in the YAML itself.

    The template is language, not code: formal names (``args`` positional,
    ``kwargs`` keyword) shadow model names inside it, and every call site is
    expanded into core AST before either backend sees the expression.
    """

    args: list[str] = []
    kwargs: list[str] = []
    template: str

    @model_validator(mode='after')
    def _check_formals(self) -> MacroDef:
        formals = [*self.args, *self.kwargs]
        if len(set(formals)) != len(formals):
            msg = f'duplicate formal names: {formals}'
            raise ValueError(msg)
        return self


class PiecewiseDef(BaseModel):
    """N expressions jointly pinned to a breakpoint-indexed piecewise curve.

    Mirrors ``linopy.Model.add_piecewise_formulation``: each link is a tuple
    ``[expression, values_parameter]`` or ``[expression, values_parameter,
    sign]``, where *expression* is any affine expression string (a bare
    variable name being the simplest), *values_parameter* names a parameter
    carrying the ``over`` dim (the breakpoint coordinates of this link), and
    *sign* bounds the link by the curve instead of pinning it (at most one
    non-``"=="``, and only with exactly two links).

    Expanded (before building) into plain variables and constraints via the
    λ convex-combination method — see ``linopy_yaml.piecewise``.
    """

    over: str  # breakpoint dimension
    links: list[list[str]]
    convex: bool = False  # True: pure-LP convex hull (no binaries)
    active: str | None = None  # gating expression: formulation pinned to 0 when 0

    @model_validator(mode='after')
    def _check_convex_shape(self) -> PiecewiseDef:
        if self.convex and len(self.links) != 2:
            msg = (
                'convex: true requires exactly two links (the hull relaxation '
                'is only well-defined for a single y=f(x) curve).'
            )
            raise ValueError(msg)
        if self.convex and self.active is not None:
            msg = 'active gating is not supported with convex: true.'
            raise ValueError(msg)
        return self

    @field_validator('links')
    @classmethod
    def _check_links(cls, v: list[list[str]]) -> list[list[str]]:
        if len(v) < 2:
            msg = 'piecewise needs at least two links ([expression, values, sign?]).'
            raise ValueError(msg)
        signs = []
        for link in v:
            if not 2 <= len(link) <= 3:
                msg = f'each link must be [expression, values] or [expression, values, sign], got {link!r}'
                raise ValueError(msg)
            sign = link[2] if len(link) == 3 else '=='
            if sign not in ('==', '<=', '>='):
                msg = f"link sign must be '==', '<=' or '>=', got {sign!r}"
                raise ValueError(msg)
            signs.append(sign)
        non_eq = [s for s in signs if s != '==']
        if len(non_eq) > 1:
            msg = "at most one link may carry a non-'==' sign."
            raise ValueError(msg)
        if non_eq and len(v) != 2:
            msg = "a non-'==' sign is only supported with exactly two links."
            raise ValueError(msg)
        return v


class MathSchema(BaseModel):
    """Top-level schema for a linopy_yaml YAML file."""

    dimensions: dict[str, DimensionDef] = {}
    parameters: dict[str, ParameterDef] = {}
    variables: dict[str, VariableDef] = {}
    constraints: dict[str, ConstraintDef] = {}
    objectives: dict[str, ObjectiveDef] = {}
    expressions: dict[str, str] = {}
    macros: dict[str, MacroDef] = {}
    piecewise: dict[str, PiecewiseDef] = {}

    @model_validator(mode='after')
    def _validate_references(self) -> MathSchema:
        errors = []

        # One flat namespace: shadowing would let a new declaration silently
        # change what an existing expression means. See resolution.py.
        kinds: list[tuple[str, Iterable[str]]] = [
            ('dimension', self.dimensions),
            ('parameter', self.parameters),
            ('variable', self.variables),
            ('named expression', self.expressions),
            ('macro', self.macros),
        ]
        seen: dict[str, str] = {}
        for kind, group in kinds:
            for name in group:
                if name in BUILTIN_NAMES:
                    errors.append(
                        f"{kind.capitalize()} '{name}' collides with the built-in helper "
                        f"'{name}'. The helper set is closed and its names are reserved; "
                        f'rename the {kind}.'
                    )
                if name in seen:
                    errors.append(
                        f"{kind.capitalize()} '{name}' collides with the {seen[name]} of "
                        f'the same name. Names share one flat namespace — rename one of '
                        f'them, so that every name in an expression or where string has '
                        f'exactly one meaning.'
                    )
                else:
                    seen[name] = kind

        # Referenced dimensions must be declared
        errors.extend(
            f"{kind} '{name}' references undeclared dimension '{d}'. Declare it under 'dimensions:'."
            for kind, group, dims_of in (
                ('Parameter', self.parameters, lambda p: p.dims),
                ('Variable', self.variables, lambda v: v.foreach),
                ('Constraint', self.constraints, lambda c: c.foreach),
            )
            for name, item in group.items()
            for d in dims_of(item)
            if d not in self.dimensions
        )

        # Bounds look like the expression language but are not it, so the
        # error says what they actually accept.
        for vname, vdef in self.variables.items():
            for side in ('lower', 'upper'):
                val = getattr(vdef.bounds, side)
                if isinstance(val, str) and val not in self.parameters:
                    looks_like_expression = not val.isidentifier()
                    detail = (
                        f'bounds accept a parameter name or a number, not an expression '
                        f'(got {val!r}). Precompute it as a parameter'
                        if looks_like_expression
                        else f"'{val}' is not a declared parameter"
                    )
                    errors.append(f"Variable '{vname}' bounds.{side}: {detail}.")

        if errors:
            raise ValueError('\n'.join(errors))

        return self
