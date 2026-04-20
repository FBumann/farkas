"""Tests for the YAML accessor and extend() behavior."""

from __future__ import annotations

import pandas as pd
import pytest
import xarray as xr

from linopy_yaml import Model
from linopy_yaml.accessor import YamlAccessor
from linopy_yaml.schema import MathSchema


def _model_with_coords(coords: dict[str, pd.Index]) -> Model:
    """Build a fresh Model with a bare YamlAccessor carrying the given coords."""
    model = Model()
    schema = MathSchema.model_validate({})
    model._yaml = YamlAccessor(model, schema, xr.Dataset(), coords)
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


def test_yaml_on_plain_model_raises():
    """Accessing .yaml on a model not built from YAML raises AttributeError."""
    m = Model()
    with pytest.raises(AttributeError, match="not built from YAML"):
        _ = m.yaml
