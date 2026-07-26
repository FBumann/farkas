"""Pydantic models for YAML schema validation."""

from __future__ import annotations

import difflib
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from linopy_yaml.helpers import BUILTIN_NAMES

if TYPE_CHECKING:
    from collections.abc import Iterable


class _StrictBlock(BaseModel):
    """Base for every schema model: unknown keys are an error, not a shrug.

    Without this, a misspelled optional key is silently dropped and the
    declaration it belonged to falls back to its default — ``boundz:`` leaves
    the variable unbounded, ``wher:`` leaves it unmasked. Both build a model
    the file does not describe, and neither says anything.
    """

    model_config = ConfigDict(extra='forbid')

    #: What this model is called in a YAML file, for the error message.
    _label: ClassVar[str] = ''

    @model_validator(mode='before')
    @classmethod
    def _reject_unknown_keys(cls, data: Any) -> Any:
        # pydantic's own extra='forbid' is the backstop; this runs first only
        # to name the near-miss, which is what a typo actually needs.
        if not isinstance(data, dict):
            return data
        known = set(cls.model_fields)
        unknown = [k for k in data if isinstance(k, str) and k not in known]
        if unknown:
            raise ValueError('\n'.join(cls._unknown_key_error(k, known) for k in unknown))
        return data

    @classmethod
    def _unknown_key_error(cls, key: str, known: set[str]) -> str:
        label = cls._label or cls.__name__
        near = difflib.get_close_matches(key, sorted(known), n=1, cutoff=0.6)
        fix = f"Did you mean '{near[0]}'?" if near else f'Valid keys: {", ".join(sorted(known))}.'
        return f"unknown key '{key}' in {label}. {fix}"


class DimensionBlock(_StrictBlock):
    """A declared dimension with optional dtype, values and coordinates.

    ``coords`` names non-index coordinates carried alongside this dimension's
    labels — a generator's bus, a line's endpoints, a snapshot's month. Each
    maps a coordinate name to the dimension its *values* are labels of, which
    is what makes ``group_sum(x, over=..., by=...)`` checkable: the values are
    verified to be coordinates of that dimension once data is bound, instead of
    being joined blind. Written either as a list, when the coordinate is named
    after its target dimension::

        generator:
          coords: [bus]

    or as a mapping, when it is not — including two coordinates onto one
    dimension::

        line:
          coords: {from: bus, to: bus}
    """

    _label: ClassVar[str] = 'a dimension declaration'

    dtype: str = 'str'
    values: list[Any] | None = None
    coords: dict[str, str] = Field(default_factory=dict)

    @field_validator('coords', mode='before')
    @classmethod
    def _normalise_coords(cls, v: Any) -> Any:
        """``[bus]`` is shorthand for ``{bus: bus}``."""
        if isinstance(v, list):
            bad = [x for x in v if not isinstance(x, str)]
            if bad:
                msg = f'coords list entries must be coordinate names, got {bad!r}'
                raise ValueError(msg)
            return {name: name for name in v}
        return v

    @field_validator('dtype')
    @classmethod
    def _check_dtype(cls, v: str) -> str:
        allowed = {'float', 'int', 'str', 'datetime'}
        if v not in allowed:
            msg = f"dtype must be one of {allowed}, got '{v}'"
            raise ValueError(msg)
        return v


class ParameterBlock(_StrictBlock):
    """A declared parameter with dims and dtype."""

    _label: ClassVar[str] = 'a parameter declaration'

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


class BoundsBlock(_StrictBlock):
    """Variable bounds — each side is a number or parameter name.

    The defaults are linopy's (``add_variables(lower=-inf, upper=inf)``): a
    declaration that omits a bound means the variable is unbounded on that
    side, not implicitly non-negative. Non-negativity is a real constraint,
    so the file has to say it.
    """

    _label: ClassVar[str] = 'a bounds block'

    lower: float | str = float('-inf')
    upper: float | str = float('inf')


class VariableBlock(_StrictBlock):
    """A declared decision variable."""

    _label: ClassVar[str] = 'a variable declaration'

    foreach: list[str]
    where: str | None = None
    bounds: BoundsBlock = BoundsBlock()
    binary: bool = False
    integer: bool = False

    @model_validator(mode='after')
    def _check_binary_integer(self) -> VariableBlock:
        if self.binary and self.integer:
            msg = 'A variable cannot be both binary and integer.'
            raise ValueError(msg)
        return self


class EquationBlock(_StrictBlock):
    """A single equation inside a constraint or objective."""

    _label: ClassVar[str] = 'an equation'

    expression: str
    where: str | None = None


class ConstraintBlock(_StrictBlock):
    """A declared constraint with foreach, where, and equations."""

    _label: ClassVar[str] = 'a constraint declaration'

    foreach: list[str]
    where: str | None = None
    equations: list[EquationBlock]

    @field_validator('equations')
    @classmethod
    def _at_least_one(cls, v: list[EquationBlock]) -> list[EquationBlock]:
        if not v:
            msg = 'A constraint must have at least one equation.'
            raise ValueError(msg)
        return v


class ObjectiveBlock(_StrictBlock):
    """A declared objective function."""

    _label: ClassVar[str] = 'an objective declaration'

    sense: str = 'minimize'
    equations: list[EquationBlock]

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
    def _at_least_one(cls, v: list[EquationBlock]) -> list[EquationBlock]:
        if not v:
            msg = 'An objective must have at least one equation.'
            raise ValueError(msg)
        return v


class MacroBlock(_StrictBlock):
    """A parameterised expression template, defined in the YAML itself.

    The template is language, not code: formal names (``args`` positional,
    ``kwargs`` keyword) shadow model names inside it, and every call site is
    expanded into core AST before either backend sees the expression.
    """

    _label: ClassVar[str] = 'a macro declaration'

    args: list[str] = []
    kwargs: list[str] = []
    template: str

    @model_validator(mode='after')
    def _check_formals(self) -> MacroBlock:
        formals = [*self.args, *self.kwargs]
        if len(set(formals)) != len(formals):
            msg = f'duplicate formal names: {formals}'
            raise ValueError(msg)
        return self


class PiecewiseBlock(_StrictBlock):
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

    _label: ClassVar[str] = 'a piecewise declaration'

    over: str  # breakpoint dimension
    links: list[list[str]]
    convex: bool = False  # True: pure-LP convex hull (no binaries)
    active: str | None = None  # gating expression: formulation pinned to 0 when 0

    @model_validator(mode='after')
    def _check_convex_shape(self) -> PiecewiseBlock:
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


class MathSchema(_StrictBlock):
    """Top-level schema for a linopy_yaml YAML file."""

    _label: ClassVar[str] = 'the top level of the file'

    dimensions: dict[str, DimensionBlock] = {}
    parameters: dict[str, ParameterBlock] = {}
    variables: dict[str, VariableBlock] = {}
    constraints: dict[str, ConstraintBlock] = {}
    objectives: dict[str, ObjectiveBlock] = {}
    expressions: dict[str, str] = {}
    macros: dict[str, MacroBlock] = {}
    piecewise: dict[str, PiecewiseBlock] = {}

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

        # A coordinate's target must be a declared dimension, and must not be
        # the dimension carrying it: grouping a dim into itself is a no-op that
        # would read as a reduction.
        for dname, ddef in self.dimensions.items():
            for cname, target in ddef.coords.items():
                if target not in self.dimensions:
                    errors.append(
                        f"Dimension '{dname}' coordinate '{cname}' targets undeclared "
                        f"dimension '{target}'. Declare it under 'dimensions:' — the "
                        f'target is what the coordinate values are checked against.'
                    )
                elif target == dname:
                    errors.append(
                        f"Dimension '{dname}' coordinate '{cname}' targets '{dname}' "
                        f'itself. A coordinate must map into a different dimension.'
                    )
                if cname in self.dimensions and cname != target:
                    errors.append(
                        f"Dimension '{dname}' coordinate '{cname}' shadows the dimension "
                        f"of the same name while targeting '{target}'. Rename the "
                        f'coordinate so a reader cannot mistake one for the other.'
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
