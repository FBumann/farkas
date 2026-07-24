"""Pydantic models for YAML schema validation."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, field_validator, model_validator


class DimensionDef(BaseModel):
    """A declared dimension with optional dtype and values."""

    dtype: str = "str"
    values: list[Any] | None = None

    @field_validator("dtype")
    @classmethod
    def _check_dtype(cls, v: str) -> str:
        allowed = {"float", "int", "str", "datetime"}
        if v not in allowed:
            msg = f"dtype must be one of {allowed}, got '{v}'"
            raise ValueError(msg)
        return v


class ParameterDef(BaseModel):
    """A declared parameter with dims and dtype."""

    dims: list[str]
    dtype: str = "float"

    @field_validator("dtype")
    @classmethod
    def _check_dtype(cls, v: str) -> str:
        allowed = {"float", "int", "bool", "str"}
        if v not in allowed:
            msg = f"dtype must be one of {allowed}, got '{v}'"
            raise ValueError(msg)
        return v


class BoundsDef(BaseModel):
    """Variable bounds — each side is a number or parameter name."""

    lower: float | str = 0
    upper: float | str = float("inf")


class VariableDef(BaseModel):
    """A declared decision variable."""

    foreach: list[str]
    where: str | None = None
    bounds: BoundsDef = BoundsDef()
    binary: bool = False
    integer: bool = False

    @model_validator(mode="after")
    def _check_binary_integer(self) -> VariableDef:
        if self.binary and self.integer:
            msg = "A variable cannot be both binary and integer."
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

    @field_validator("equations")
    @classmethod
    def _at_least_one(cls, v: list[EquationDef]) -> list[EquationDef]:
        if not v:
            msg = "A constraint must have at least one equation."
            raise ValueError(msg)
        return v


class ObjectiveDef(BaseModel):
    """A declared objective function."""

    sense: str = "minimize"
    equations: list[EquationDef]

    @field_validator("sense")
    @classmethod
    def _check_sense(cls, v: str) -> str:
        allowed = {"minimize", "maximize"}
        if v not in allowed:
            msg = f"sense must be one of {allowed}, got '{v}'"
            raise ValueError(msg)
        return v

    @field_validator("equations")
    @classmethod
    def _at_least_one(cls, v: list[EquationDef]) -> list[EquationDef]:
        if not v:
            msg = "An objective must have at least one equation."
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

    @model_validator(mode="after")
    def _validate_references(self) -> MathSchema:
        errors = []

        # Named expressions must not shadow other model names
        for ename in self.expressions:
            for kind, names in (
                ("parameter", self.parameters),
                ("variable", self.variables),
                ("dimension", self.dimensions),
            ):
                if ename in names:
                    errors.append(
                        f"Named expression '{ename}' collides with a declared "
                        f"{kind} of the same name. Rename one of them."
                    )

        # Check parameter dims reference declared dimensions
        for pname, pdef in self.parameters.items():
            for d in pdef.dims:
                if d not in self.dimensions:
                    errors.append(
                        f"Parameter '{pname}' references undeclared "
                        f"dimension '{d}'. Declare it under 'dimensions:'."
                    )

        # Check variable foreach references declared dimensions
        for vname, vdef in self.variables.items():
            for d in vdef.foreach:
                if d not in self.dimensions:
                    errors.append(
                        f"Variable '{vname}' references undeclared "
                        f"dimension '{d}'. Declare it under 'dimensions:'."
                    )

        # Check constraint foreach references declared dimensions
        for cname, cdef in self.constraints.items():
            for d in cdef.foreach:
                if d not in self.dimensions:
                    errors.append(
                        f"Constraint '{cname}' references undeclared "
                        f"dimension '{d}'. Declare it under 'dimensions:'."
                    )

        # Check variable bounds parameter references
        for vname, vdef in self.variables.items():
            for side in ("lower", "upper"):
                val = getattr(vdef.bounds, side)
                if isinstance(val, str) and val not in self.parameters:
                    errors.append(
                        f"Variable '{vname}' bounds.{side} references "
                        f"undeclared parameter '{val}'."
                    )

        if errors:
            raise ValueError("\n".join(errors))

        return self
