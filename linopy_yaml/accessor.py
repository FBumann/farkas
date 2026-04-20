"""YAML accessor attached to Model instances as ``model.yaml``."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

import pandas as pd
import xarray as xr
import yaml

from linopy_yaml.builder import build_model
from linopy_yaml.loader import build_master_coords, load_parameters
from linopy_yaml.schema import MathSchema

if TYPE_CHECKING:
    import linopy


class YamlAccessor:
    """Accessor attached to a Model instance as ``model.yaml``.

    Provides access to the parsed YAML schema, loaded parameter dataset,
    master coordinates, and the ability to add more math from another YAML.
    """

    def __init__(
        self,
        model: "linopy.Model",
        schema: MathSchema,
        dataset: xr.Dataset,
        coords: dict[str, pd.Index],
    ) -> None:
        self._model = model
        self._schema = schema
        self._dataset = dataset
        self._coords = coords

    @property
    def schema(self) -> MathSchema:
        """The parsed YAML math definition."""
        return self._schema

    @property
    def dataset(self) -> xr.Dataset:
        """The loaded parameter dataset."""
        return self._dataset

    @property
    def coords(self) -> dict[str, pd.Index]:
        """The master coordinates for all declared dimensions."""
        return dict(self._coords)

    def extend(
        self,
        path: str | Path,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Add variables, constraints, and/or objectives from another YAML.

        The second YAML may reference dimensions and parameters already
        loaded. New parameters can be provided via ``data=``.

        Parameters
        ----------
        path : str or Path
            Path to the additional YAML file.
        data : dict or None
            Additional parameter data for new parameters in this YAML.
        """
        path = Path(path)
        raw = yaml.safe_load(path.read_text())
        if raw is None:
            raw = {}

        schema = MathSchema.model_validate(raw)

        # If the extension declares values: for a dimension already resolved
        # by the existing model, require them to match. Silent-ignore would
        # hide real mismatches.
        for dim_name, dim_def in schema.dimensions.items():
            if dim_def.values is None or dim_name not in self._coords:
                continue
            declared = pd.Index(dim_def.values, name=dim_name)
            existing = self._coords[dim_name]
            if not declared.equals(existing):
                msg = (
                    f"Extension declares dimension '{dim_name}' with values "
                    f"that differ from the existing model.\n"
                    f"  Existing: {list(existing)}\n"
                    f"  Declared: {list(declared)}\n"
                    f"Either omit 'values:' for '{dim_name}' in the "
                    f"extension, or make them match."
                )
                raise ValueError(msg)

        # Merge master coords: existing + any new dimensions
        merged_coords = dict(self._coords)
        new_coords = build_master_coords(schema, None)
        for dim, idx in new_coords.items():
            if dim not in merged_coords:
                merged_coords[dim] = idx

        # Load new parameters and merge with existing dataset
        new_dataset = load_parameters(schema, data, merged_coords)
        merged_dataset = self._dataset.merge(new_dataset, compat="override")

        # Build new components
        build_model(self._model, schema, merged_dataset, merged_coords)

        # Update stored state
        self._coords = merged_coords
        self._dataset = merged_dataset
