"""Tests for Exception.add_note() context attached by from_yaml/extend/builder."""

from __future__ import annotations

import pandas as pd
import pytest
import xarray as xr

import linopy_yaml  # noqa: F401 — registers .from_yaml / .yaml on linopy.Model
from linopy import Model
from linopy_yaml._patch import _ACCESSOR_REGISTRY
from linopy_yaml.accessor import YamlAccessor
from linopy_yaml.schema import MathSchema


def _has_note(exc: BaseException, substring: str) -> bool:
    return any(substring in n for n in getattr(exc, "__notes__", []))


def test_from_yaml_attaches_path_and_variable_notes(tmp_path):
    """A failure inside _build_variables stacks the variable name and YAML path."""
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "dimensions:\n"
        "  g: {values: [a]}\n"
        "variables:\n"
        "  p:\n"
        "    foreach: [g]\n"
        "    where: '<<<'\n"  # malformed where string -> parse error at build time
    )

    with pytest.raises(ValueError) as ei:
        Model.from_yaml(bad)

    assert _has_note(ei.value, "while building variable 'p'")
    assert _has_note(ei.value, f"while loading YAML '{bad}'")


def test_from_yaml_attaches_constraint_note(tmp_path):
    """A failure inside _build_constraints carries the constraint name."""
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "dimensions:\n"
        "  g: {values: [a]}\n"
        "variables:\n"
        "  p:\n"
        "    foreach: [g]\n"
        "constraints:\n"
        "  c:\n"
        "    foreach: [g]\n"
        "    equations:\n"
        "      - expression: 'p + 1'\n"  # no comparison operator
    )

    with pytest.raises(ValueError) as ei:
        Model.from_yaml(bad)

    assert _has_note(ei.value, "while building constraint 'c'")


def test_from_yaml_attaches_objective_note(tmp_path):
    """A failure inside _build_objectives carries the objective name."""
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "dimensions:\n"
        "  g: {values: [a]}\n"
        "variables:\n"
        "  p:\n"
        "    foreach: [g]\n"
        "objectives:\n"
        "  obj:\n"
        "    equations:\n"
        "      - expression: 'p == 1'\n"  # objectives forbid comparison operators
    )

    with pytest.raises(ValueError) as ei:
        Model.from_yaml(bad)

    assert _has_note(ei.value, "while building objective 'obj'")


def test_extend_attaches_path_note(tmp_path):
    """A failure inside extend() carries the extension YAML path."""
    model = Model()
    _ACCESSOR_REGISTRY[model] = YamlAccessor(
        model,
        schema=MathSchema.model_validate({}),
        dataset=xr.Dataset(),
        coords={"time": pd.Index([0, 1, 2, 3], name="time")},
    )

    ext = tmp_path / "ext.yaml"
    ext.write_text(
        "dimensions:\n"
        "  time:\n"
        "    values: [a, b]\n"  # mismatched values trigger the existing-coords check
    )

    with pytest.raises(ValueError) as ei:
        model.yaml.extend(ext)

    assert _has_note(ei.value, f"while extending with YAML '{ext}'")
