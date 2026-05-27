"""YAML accessor attached to Model instances as ``model.yaml``."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import xarray as xr
import yaml

from linopy_yaml._notes import note
from linopy_yaml.builder import build_model
from linopy_yaml.loader import build_master_coords, load_parameters
from linopy_yaml.schema import MathSchema

if TYPE_CHECKING:
    import linopy


def _infer_coords(model: linopy.Model) -> dict[str, pd.Index]:
    """Union the coordinates of every variable on ``model``, keyed by dim.

    Best-effort live view of "what dimensions exist on this model right now".
    Used by ``YamlAccessor.coords`` as a fallback for dims that are not
    explicitly declared via YAML or a ``coords=`` kwarg.

    Delegates to ``model.variables.indexes``, which is linopy's public API
    for the per-dimension union of coordinates across all variables.
    """
    with warnings.catch_warnings():
        # linopy emits a UserWarning when variables have non-aligned coords
        # and performs an outer join. That outer join is exactly the union
        # semantics we want here, so silence the noise for this call.
        warnings.filterwarnings(
            "ignore",
            message="Coordinates across variables not equal",
            category=UserWarning,
        )
        return dict(model.variables.indexes)


class YamlAccessor:
    """Accessor attached to a Model instance as ``model.yaml``.

    Covers the YAML-managed portion of the model — not the whole model.
    On Python-built models the accessor is lazily initialised with an
    empty schema and dataset; ``.coords`` falls back to live inference
    from ``model.variables``.
    """

    def __init__(
        self,
        model: linopy.Model,
        schema: MathSchema | None,
        dataset: xr.Dataset,
        coords: dict[str, pd.Index],
    ) -> None:
        self._model = model
        self._schema = schema
        self._dataset = dataset
        self._declared_coords: dict[str, pd.Index] = dict(coords)

    @property
    def schema(self) -> MathSchema | None:
        """Parsed YAML math definition, or ``None`` if no YAML has been loaded."""
        return self._schema

    @property
    def dataset(self) -> xr.Dataset:
        """Loaded parameter dataset (empty for Python-built models)."""
        return self._dataset

    @property
    def coords(self) -> dict[str, pd.Index]:
        """Master coordinates: live model inference merged with declared coords.

        Declared coords (from YAML ``values:`` or a ``coords=`` kwarg) win
        over inference. Inference is recomputed at every access, so newly
        added Python variables appear immediately.
        """
        merged = _infer_coords(self._model)
        merged.update(self._declared_coords)
        return merged

    def extend(
        self,
        path: str | Path,
        *,
        data: dict[str, Any] | None = None,
        coords: dict[str, Any] | None = None,
    ) -> None:
        """Add variables, constraints, and/or objectives from another YAML.

        Parameters
        ----------
        path : str or Path
            Path to the additional YAML file.
        data : dict or None
            Additional parameter data for new parameters in this YAML.
        coords : dict or None
            Coordinate overrides for this call. Highest precedence — beats
            both existing model coords and ``values:`` declared in the
            extension YAML.

        Coords precedence (highest first):

        1. ``coords=`` kwarg to this call
        2. Coords already declared by prior YAML or a prior ``coords=`` kwarg
        3. Coords inferred from existing model variables
        4. ``values:`` declared in the extension YAML
        5. Error if none of the above provide values for a referenced dim
        """
        path = Path(path)
        with note(f"while extending with YAML '{path}'"):
            raw = yaml.safe_load(path.read_text())
            if raw is None:
                raw = {}

            schema = MathSchema.model_validate(raw)

            kwarg_coords: dict[str, pd.Index] = {}
            if coords is not None:
                for k, v in coords.items():
                    kwarg_coords[k] = pd.Index(v, name=k)

            # Everything we know about coords going into this extend, in
            # precedence order: kwarg > prior declared > inferred.
            known = _infer_coords(self._model)
            known.update(self._declared_coords)
            known.update(kwarg_coords)

            # Mismatch check: if the extension YAML declares values: for a dim
            # we already know about, the values must match. Silent override
            # would hide real bugs.
            for dim_name, dim_def in schema.dimensions.items():
                if dim_def.values is None or dim_name not in known:
                    continue
                declared = pd.Index(dim_def.values, name=dim_name)
                existing = known[dim_name]
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

            # ``known`` is passed as the override to build_master_coords, so any
            # dim it covers takes precedence over the extension's ``values:``.
            # Dims still missing fall through to ``values:`` or raise.
            master_coords = build_master_coords(schema, known)

            new_dataset = load_parameters(schema, data, master_coords)
            merged_dataset = self._dataset.merge(new_dataset, compat="override")

            build_model(self._model, schema, merged_dataset, master_coords)

            # Persist explicit user statements about coords. Inferred dims are
            # deliberately left unstored so ``self.coords`` keeps reflecting
            # whatever the model currently has.
            for dim_name, dim_def in schema.dimensions.items():
                if dim_def.values is not None:
                    self._declared_coords[dim_name] = pd.Index(
                        dim_def.values, name=dim_name
                    )
            for dim, idx in kwarg_coords.items():
                self._declared_coords[dim] = idx

            self._dataset = merged_dataset
            if self._schema is None:
                self._schema = schema
