"""Tests for the YAML accessor and extend() behavior."""

from __future__ import annotations

import pandas as pd
import pytest
import xarray as xr

import linopy_yaml  # noqa: F401 — registers .from_yaml / .yaml on linopy.Model
from linopy import Model
from linopy_yaml._patch import _ACCESSOR_REGISTRY
from linopy_yaml.accessor import YamlAccessor, _infer_coords
from linopy_yaml.schema import MathSchema


def _model_with_coords(coords: dict[str, pd.Index]) -> Model:
    """Build a fresh Model with a bare YamlAccessor carrying the given coords."""
    model = Model()
    schema = MathSchema.model_validate({})
    _ACCESSOR_REGISTRY[model] = YamlAccessor(model, schema, xr.Dataset(), coords)
    return model


def test_extend_rejects_mismatched_dim_values(tmp_path):
    """Extension declaring values: for an existing dim must match exactly."""
    model = _model_with_coords({"time": pd.Index([0, 1, 2, 3], name="time")})

    ext = tmp_path / "ext.yaml"
    ext.write_text(
        "dimensions:\n"
        "  time:\n"
        "    values: [a, b]\n"
    )

    with pytest.raises(ValueError, match="differ from the existing model"):
        model.yaml.extend(ext)


def test_extend_accepts_matching_dim_values(tmp_path):
    """Extension may redeclare values: as long as they match exactly."""
    model = _model_with_coords(
        {"generator": pd.Index(["wind", "solar"], name="generator")}
    )

    ext = tmp_path / "ext.yaml"
    ext.write_text(
        "dimensions:\n"
        "  generator:\n"
        "    values: [wind, solar]\n"
    )

    # Should not raise from our coords check. It may still raise later from
    # the build step, but not with the mismatch message.
    try:
        model.yaml.extend(ext)
    except ValueError as e:
        assert "differ from the existing model" not in str(e)


def test_yaml_on_plain_model_lazy_inits():
    """Accessing .yaml on a model not built from YAML returns an empty accessor."""
    m = Model()
    acc = m.yaml
    assert acc.schema is None
    assert len(acc.dataset.data_vars) == 0
    assert acc.coords == {}


def test_yaml_lazy_init_is_idempotent():
    """The same accessor is returned on subsequent .yaml accesses."""
    m = Model()
    assert m.yaml is m.yaml


def test_coords_is_live_view_of_model_variables():
    """Variables added to the model after lazy init show up in .coords."""
    m = Model()
    _ = m.yaml  # force lazy init with empty declared coords

    generators = pd.Index(["wind", "solar"], name="generator")
    m.add_variables(name="x", coords=[generators])

    assert "generator" in m.yaml.coords
    assert list(m.yaml.coords["generator"]) == ["wind", "solar"]


def test_infer_coords_unions_across_variables():
    """_infer_coords unions per-dim coordinates across all model variables."""
    m = Model()
    m.add_variables(
        name="a", coords=[pd.Index(["wind", "solar"], name="generator")]
    )
    m.add_variables(
        name="b", coords=[pd.Index(["wind", "gas"], name="generator")]
    )

    inferred = _infer_coords(m)
    assert "generator" in inferred
    assert set(inferred["generator"]) == {"wind", "solar", "gas"}


def test_extend_uses_inferred_coords_when_yaml_omits_values(tmp_path):
    """Extension YAML may omit values: for dims already on the model."""
    m = Model()
    m.add_variables(
        name="p", coords=[pd.Index(["wind", "solar"], name="generator")]
    )

    ext = tmp_path / "ext.yaml"
    ext.write_text(
        "dimensions:\n"
        "  generator: {}\n"
        "parameters:\n"
        "  cap:\n"
        "    dims: [generator]\n"
    )

    m.yaml.extend(ext, data={"cap": pd.Series({"wind": 1.0, "solar": 2.0})})

    assert "cap" in m.yaml.dataset.data_vars
    assert list(m.yaml.coords["generator"]) == ["wind", "solar"]


def test_extend_rejects_yaml_values_that_disagree_with_inferred(tmp_path):
    """Extension values: must match inferred coords too, not just declared."""
    m = Model()
    m.add_variables(
        name="p", coords=[pd.Index(["wind", "solar"], name="generator")]
    )

    ext = tmp_path / "ext.yaml"
    ext.write_text(
        "dimensions:\n"
        "  generator:\n"
        "    values: [wind, gas]\n"
    )

    with pytest.raises(ValueError, match="differ from the existing model"):
        m.yaml.extend(ext)


def test_extend_coords_kwarg_overrides_inferred(tmp_path):
    """coords= kwarg to extend() wins over inference from model variables."""
    m = Model()
    m.add_variables(
        name="p", coords=[pd.Index(["wind", "solar"], name="generator")]
    )

    ext = tmp_path / "ext.yaml"
    ext.write_text(
        "dimensions:\n"
        "  generator: {}\n"
        "parameters:\n"
        "  cap:\n"
        "    dims: [generator]\n"
    )

    # Override: declare a different generator set just for this extend.
    m.yaml.extend(
        ext,
        data={"cap": pd.Series({"wind": 1.0, "gas": 3.0})},
        coords={"generator": ["wind", "gas"]},
    )

    # The override is now a declared coord and wins over inference.
    assert list(m.yaml.coords["generator"]) == ["wind", "gas"]


def test_extend_sets_schema_on_python_built_model(tmp_path):
    """First extend() on a Python-built model populates .schema."""
    m = Model()
    assert m.yaml.schema is None

    ext = tmp_path / "ext.yaml"
    ext.write_text(
        "dimensions:\n"
        "  generator:\n"
        "    values: [wind, solar]\n"
        "parameters:\n"
        "  cap:\n"
        "    dims: [generator]\n"
    )

    m.yaml.extend(ext, data={"cap": pd.Series({"wind": 1.0, "solar": 2.0})})
    assert m.yaml.schema is not None
    assert "cap" in m.yaml.schema.parameters
