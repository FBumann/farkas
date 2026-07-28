"""Compatibility shim: YAML math onto a ``linopy.Model``.

Requires the ``[linopy]`` extra (linopy, xarray).

The product path is YAML → AST → streaming engine; linopy is not on it. This
module exists for two narrow jobs:

1. **Python math that the language cannot say.** Build (or extend) a model in
   linopy, where arbitrary Python is available.
2. **Parity checking.** Every language feature is differentially tested by
   running the same YAML + data through both this and the streaming engine.
   That is only meaningful because both accept *exactly* the same language —
   there is no construct that works here and not there.

Two functions, and they are **pure producers**: YAML goes in, a model comes
out, and nothing is retained. No accessor, no session, no state on the model.
A file's meaning never depends on what was loaded before it (docs/ARCHITECTURE.md,
hard rule 5), so every file declares the parameters it uses and the caller
supplies their data per call::

    from farkas import linopy as farkas_linopy

    m = farkas_linopy.build('model.yaml', data={...})
    farkas_linopy.extend(m, 'ramp_constraint.yaml', data={...})

For models declared entirely in YAML, use the native API — it streams::

    import farkas as fk

    with fk.solve('model.yaml', {...}) as result:
        result.primal('p')
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

try:
    import linopy
    import pandas as pd
    import xarray  # noqa: F401 — guarded here so the message covers it
except ModuleNotFoundError as exc:  # linopy / xarray absent
    msg = 'The linopy compatibility layer requires the [linopy] extra: pip install "farkas[linopy]"'
    raise ModuleNotFoundError(msg) from exc

from farkas._notes import note
from farkas._yaml import read_yaml
from farkas.errors import LanguageError
from farkas.linopy.builder import build_model
from farkas.linopy.loader import build_dim_coords, build_master_coords, dim_index_of, load_parameters
from farkas.piecewise import expand_piecewise, validate_piecewise_data
from farkas.schema import MathSchema
from farkas.validation import validate_expressions

__all__ = ['build', 'extend']


def build(
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
    data : mapping or None
        Parameter data. Keys are parameter names declared in the YAML.
    coords : mapping or None
        Dimension coordinate values. Overrides ``values:`` declared in YAML.

    Raises
    ------
    ValueError
        For any validation failure (missing dimensions, parameters, etc.).
    pydantic.ValidationError
        If the YAML structure is invalid.
    """
    path = Path(path)
    with note(f"while loading YAML '{path}'"):
        original = _read(path)
        schema = expand_piecewise(original)
        validate_expressions(schema)

        master_coords = build_master_coords(schema, coords)
        dim_coords = build_dim_coords(schema, coords, master_coords)
        dataset = load_parameters(schema, data, master_coords)
        validate_piecewise_data(original, dataset)

        model = linopy.Model()
        build_model(model, schema, dataset, master_coords, dim_coords)

    return model


def extend(
    model: linopy.Model,
    path: str | Path,
    *,
    data: dict[str, Any] | None = None,
    coords: dict[str, Any] | None = None,
) -> None:
    """Add variables, constraints, and/or objectives from YAML to *model*.

    Mutates *model* in place. Expressions may reference variables already on
    the model — those come from the model itself, not from prior calls. The
    YAML must declare every parameter it uses, and this call must supply that
    parameter's data.

    Coords precedence (highest first):

    1. ``coords=`` kwarg to this call
    2. coords inferred from the model's existing variables
    3. ``values:`` declared in this YAML
    4. error if none of the above resolve a referenced dim
    """
    path = Path(path)
    with note(f"while extending with YAML '{path}'"):
        original = _read(path)
        schema = expand_piecewise(original)
        validate_expressions(
            schema,
            # linopy dims are Hashable; the language's are names
            known_variables={n: [str(d) for d in model.variables[n].dims] for n in model.variables},
        )

        known = _infer_coords(model)
        if coords is not None:
            known.update({k: dim_index_of(v, k) for k, v in coords.items()})

        # If this YAML declares values: for a dim the model already has, they
        # must match. Silent override would hide real bugs.
        for dim_name, dim_def in schema.dimensions.items():
            if dim_def.values is None or dim_name not in known:
                continue
            declared = pd.Index(dim_def.values, name=dim_name)
            existing = known[dim_name]
            if not declared.equals(existing):
                msg = (
                    f"Extension declares dimension '{dim_name}' with values "
                    f'that differ from the existing model.\n'
                    f'  Existing: {list(existing)}\n'
                    f'  Declared: {list(declared)}\n'
                    f"Either omit 'values:' for '{dim_name}' in the "
                    f'extension, or make them match.'
                )
                raise LanguageError(msg)

        # ``known`` is the override, so any dim it covers beats this YAML's
        # ``values:``. Dims still missing fall through to ``values:`` or raise.
        master_coords = build_master_coords(schema, known)
        dim_coords = build_dim_coords(schema, coords, master_coords)
        dataset = load_parameters(schema, data, master_coords)
        validate_piecewise_data(original, dataset)

        build_model(model, schema, dataset, master_coords, dim_coords)


def _read(path: Path) -> MathSchema:
    return MathSchema.model_validate(read_yaml(path))


def _infer_coords(model: linopy.Model) -> dict[str, pd.Index]:
    """Union the coordinates of every variable on ``model``, keyed by dim.

    Delegates to ``model.variables.indexes``, linopy's public API for the
    per-dimension union of coordinates across all variables.
    """
    with warnings.catch_warnings():
        # linopy warns when variables have non-aligned coords and performs an
        # outer join. That outer join is the union semantics we want here.
        warnings.filterwarnings(
            'ignore',
            message='Coordinates across variables not equal',
            category=UserWarning,
        )
        return dict(model.variables.indexes)
