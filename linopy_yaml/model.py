"""Model subclass with .yaml accessor and .from_yaml() classmethod.

This is a temporary workaround for linopy.Model not being weakref-able
(`__weakref__` is not in `linopy.Model.__slots__`). Once upstream linopy
adds `__weakref__` to its slots, this subclass can be removed and the
package can monkey-patch `linopy.Model` directly with a WeakKeyDictionary-
backed accessor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import xarray as xr
import yaml

import linopy

from linopy_yaml.accessor import YamlAccessor
from linopy_yaml.builder import build_model
from linopy_yaml.loader import build_master_coords, load_parameters
from linopy_yaml.schema import MathSchema


class Model(linopy.Model):
    """linopy.Model with a YAML accessor.

    Adds a single slot, ``_yaml``, to store the accessor. Instances behave
    identically to ``linopy.Model`` in every other respect.
    """

    __slots__ = ("_yaml",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._yaml = None

    @property
    def yaml(self) -> YamlAccessor:
        """Return the YAML accessor, or raise if this model has none."""
        if self._yaml is None:
            msg = (
                "This model was not built from YAML.\n"
                "Use Model.from_yaml('model.yaml', data={...}) to "
                "create a YAML-backed model."
            )
            raise AttributeError(msg)
        return self._yaml

    @classmethod
    def from_yaml(
        cls,
        path: str | Path,
        *,
        data: dict[str, Any] | None = None,
        coords: dict[str, Any] | None = None,
    ) -> Model:
        """Build a Model from a YAML math definition.

        Parameters
        ----------
        path : str or Path
            Path to the YAML file.
        data : dict or None
            Parameter data. Keys are parameter names declared in the YAML.
        coords : dict or None
            Dimension coordinate values. Overrides values declared in YAML.

        Returns
        -------
        Model
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
        model._yaml = YamlAccessor(model, schema, dataset, master_coords)

        build_model(model, schema, dataset, master_coords)

        return model
