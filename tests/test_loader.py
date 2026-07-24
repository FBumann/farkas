"""Tests for data loading, coercion, and validation."""

import pandas as pd
import pytest
import xarray as xr

from linopy_yaml.loader import build_master_coords, load_parameters
from linopy_yaml.schema import MathSchema


def _schema(dims=None, params=None):
    """Helper to build a minimal MathSchema."""
    raw = {}
    if dims:
        raw["dimensions"] = dims
    if params:
        raw["parameters"] = params
    return MathSchema.model_validate(raw)


class TestBuildMasterCoords:
    def test_from_yaml_values(self):
        s = _schema(dims={"x": {"values": [1, 2, 3]}})
        mc = build_master_coords(s, None)
        assert list(mc["x"]) == [1, 2, 3]

    def test_from_coords_kwarg(self):
        s = _schema(dims={"x": {}})
        mc = build_master_coords(s, {"x": [10, 20]})
        assert list(mc["x"]) == [10, 20]

    def test_coords_overrides_yaml(self):
        s = _schema(dims={"x": {"values": [1, 2]}})
        mc = build_master_coords(s, {"x": [99]})
        assert list(mc["x"]) == [99]

    def test_missing_raises(self):
        s = _schema(dims={"x": {}})
        with pytest.raises(ValueError, match="Dimension 'x' has no values"):
            build_master_coords(s, None)


class TestLoadParameters:
    def test_scalar_data(self):
        s = _schema(
            dims={"x": {"values": [1, 2]}},
            params={"a": {"dims": ["x"]}},
        )
        mc = build_master_coords(s, None)
        ds = load_parameters(s, {"a": 5.0}, mc)
        assert float(ds["a"].sel(x=1)) == 5.0

    def test_dict_data(self):
        s = _schema(
            dims={"g": {"values": ["wind", "solar"]}},
            params={"p": {"dims": ["g"]}},
        )
        mc = build_master_coords(s, None)
        ds = load_parameters(s, {"p": {"wind": 100, "solar": 60}}, mc)
        assert float(ds["p"].sel(g="wind")) == 100.0

    def test_series_data(self):
        s = _schema(
            dims={"g": {"values": ["a", "b"]}},
            params={"p": {"dims": ["g"]}},
        )
        mc = build_master_coords(s, None)
        series = pd.Series([1.0, 2.0], index=pd.Index(["a", "b"], name="g"))
        ds = load_parameters(s, {"p": series}, mc)
        assert float(ds["p"].sel(g="b")) == 2.0

    def test_xarray_data(self):
        s = _schema(
            dims={"x": {"values": [0, 1]}},
            params={"a": {"dims": ["x"]}},
        )
        mc = build_master_coords(s, None)
        da = xr.DataArray([10, 20], dims=["x"], coords={"x": [0, 1]})
        ds = load_parameters(s, {"a": da}, mc)
        assert float(ds["a"].sel(x=1)) == 20.0

    def test_missing_required_raises(self):
        s = _schema(
            dims={"x": {"values": [1]}},
            params={"a": {"dims": ["x"]}},
        )
        mc = build_master_coords(s, None)
        with pytest.raises(ValueError, match="required"):
            load_parameters(s, {}, mc)

    def test_unknown_keys_raises(self):
        s = _schema(dims={"x": {"values": [1]}})
        mc = build_master_coords(s, None)
        with pytest.raises(ValueError, match="not declared"):
            load_parameters(s, {"extra": 1}, mc)

    def test_unexpected_dims_raises(self):
        s = _schema(
            dims={"x": {"values": [1]}, "y": {"values": [2]}},
            params={"a": {"dims": ["x"]}},
        )
        mc = build_master_coords(s, None)
        da = xr.DataArray([[1]], dims=["x", "y"], coords={"x": [1], "y": [2]})
        with pytest.raises(ValueError, match="unexpected dimensions"):
            load_parameters(s, {"a": da}, mc)

    def test_unknown_coord_raises(self):
        s = _schema(
            dims={"g": {"values": ["a", "b"]}},
            params={"p": {"dims": ["g"]}},
        )
        mc = build_master_coords(s, None)
        series = pd.Series([1.0], index=pd.Index(["z"], name="g"))
        with pytest.raises(ValueError, match="not in the master coordinate"):
            load_parameters(s, {"p": series}, mc)
