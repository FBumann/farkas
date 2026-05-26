"""Monkey-patch ``linopy.Model`` with ``.from_yaml()`` and ``.yaml``.

Per-instance accessor state lives in a ``WeakKeyDictionary`` keyed by model,
which requires linopy>=0.7 (PyPSA/linopy#656).
"""

from __future__ import annotations

import weakref
from pathlib import Path
from typing import Any

import linopy
import xarray as xr
import yaml

from linopy_yaml.accessor import YamlAccessor
from linopy_yaml.builder import build_model
from linopy_yaml.loader import build_master_coords, load_parameters
from linopy_yaml.schema import MathSchema

_ACCESSOR_REGISTRY: weakref.WeakKeyDictionary[linopy.Model, YamlAccessor] = (
    weakref.WeakKeyDictionary()
)


class _YamlDescriptor:
    """Returns the model's ``YamlAccessor``, lazy-initialised on first access."""

    def __get__(
        self, instance: linopy.Model | None, owner: type | None = None
    ) -> YamlAccessor | _YamlDescriptor:
        if instance is None:
            return self
        accessor = _ACCESSOR_REGISTRY.get(instance)
        if accessor is None:
            accessor = YamlAccessor(
                instance,
                schema=None,
                dataset=xr.Dataset(),
                coords={},
            )
            _ACCESSOR_REGISTRY[instance] = accessor
        return accessor


def _from_yaml(
    cls: type[linopy.Model],
    path: str | Path,
    *,
    data: dict[str, Any] | None = None,
    coords: dict[str, Any] | None = None,
) -> linopy.Model:
    """Build a ``linopy.Model`` from a YAML math definition.

    Parameters
    ----------
    path : str or Path
        Path to the YAML file.
    data : dict or None
        Parameter data. Keys are parameter names declared in the YAML.
    coords : dict or None
        Dimension coordinate values. Overrides ``values:`` declared in YAML.

    Returns
    -------
    linopy.Model
        A fully built model ready to solve. Access YAML metadata via
        ``model.yaml.schema``, ``model.yaml.dataset``, etc.

    Raises
    ------
    ValueError
        For any validation failure (missing dimensions, parameters, etc.).
    pydantic.ValidationError
        If the YAML structure is invalid.
    """
    path = Path(path)
    raw = yaml.safe_load(path.read_text())
    if raw is None:
        raw = {}

    schema = MathSchema.model_validate(raw)

    master_coords = build_master_coords(schema, coords)
    dataset = load_parameters(schema, data, master_coords)

    model = cls()
    _ACCESSOR_REGISTRY[model] = YamlAccessor(model, schema, dataset, master_coords)

    build_model(model, schema, dataset, master_coords)

    return model


def apply_patches() -> None:
    """Install ``from_yaml`` and ``yaml`` on ``linopy.Model``."""
    linopy.Model.from_yaml = classmethod(_from_yaml)  # type: ignore[attr-defined]
    linopy.Model.yaml = _YamlDescriptor()  # type: ignore[attr-defined]
