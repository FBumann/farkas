"""Bind runtime data to a validated schema.

The language says what a parameter *is* — its dims, its dtype — and never
where its values come from. This module is the other half: it takes what the
caller actually passed (parquet paths, or any table exposing the Arrow
PyCapsule protocol) and produces the tidy frames the engine reads by name.

It lives here rather than in ``lowering.py`` because it is not lowering.
Lowering turns an AST into a plan and touches no data at all; this touches
only data and knows nothing about expressions. They were in one file because
``api.build`` calls them on consecutive lines, which is not a reason.

The shapes themselves are recognised in :mod:`lpspec.relational.frames`,
so no dataframe library beyond the engine's own is a dependency of either lane.

The ``piecewise:`` curvature guard lives here for the same reason. It is the
one check a schema cannot answer — convexity is a property of the breakpoint
*values* — so it belongs with the data rather than with the expansion that
emits the declarations (:mod:`lpspec.language.piecewise`). Keeping it here is
also what leaves that expansion inside ``language/``: this is the module that
already knows what shape a caller's table is in.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from lpspec.errors import DataError, PiecewiseExpansionError
from lpspec.relational.frames import as_frame, labels_frame

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lpspec.language.schema import MathSchema


def tidy_sources(
    schema: MathSchema,
    data: dict[str, object],
    coords: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Adapt the caller's ``data=``/``coords=`` inputs to executor sources.

    Every in-memory source becomes a tidy :class:`polars.LazyFrame` with columns
    ``(dims…, value)``; parquet paths pass through untouched for the engine to
    scan directly. Dimension indexes come from ``data``, ``coords``, declared YAML
    values, or fall back to the executor's inference from parameter tables.

    Normalising here rather than at the executor is what lets the piecewise
    curvature guard see every in-memory shape alike (:mod:`relational.frames`
    is where the shapes are recognised).
    """
    sources: dict[str, object] = {}
    for pname, pdef in schema.parameters.items():
        if pname not in data:
            raise DataError(f"no data provided for parameter '{pname}'")
        obj = data[pname]
        if isinstance(obj, (str, Path)):
            sources[pname] = obj  # parquet path — the executor reads it directly
            continue
        table = as_frame(obj, pdef.dims)
        if table is None:
            raise DataError(
                f"parameter '{pname}': cannot adapt {type(obj).__name__} to a tidy "
                f'table — pass any table polars can read with columns '
                f'{[*pdef.dims, "value"]} (polars, pyarrow, pandas), or a parquet path'
            )
        available = table.collect_schema().names()
        if any(d not in available for d in pdef.dims):
            raise DataError(
                f"parameter '{pname}': source columns {available} "
                f'do not match its declared dims {list(pdef.dims)}. Rename them to the '
                f'declared dims, or drop the index names to bind positionally.'
            )
        sources[pname] = table

    for dname, ddef in schema.dimensions.items():
        if dname in data:
            src = data[dname]
        elif coords and dname in coords:
            src = coords[dname]
        elif ddef.values is not None:
            src = ddef.values
        else:
            continue
        if isinstance(src, (str, Path)):
            sources[dname] = src
            continue
        table = as_frame(src, (dname,))
        sources[dname] = table if table is not None else labels_frame(dname, src)

    validate_piecewise_data(schema, sources)

    return sources


def validate_piecewise_data(schema: MathSchema, values: Mapping[str, Any] | Any) -> None:
    """Data-time guard for ``convex: true`` blocks (SPEC §3.6).

    The hull relaxation is silently wrong for curves of mixed curvature, and
    ill-defined when the x-breakpoints are not strictly monotone — with the
    breakpoint values in hand (which the schema never has), both are
    checkable. *values* maps parameter names to whatever its lane holds: the
    tidy ``pyarrow.Table`` / parquet path of :func:`tidy_sources`, or the
    linopy lane's ``xr.Dataset``. Blocks whose parameters are missing, or
    bound to a path (not readable in process), are skipped; a missing
    parameter errors elsewhere.

    Both lanes call this, which is why it sits beside ``tidy_sources`` rather
    than inside either of them.
    """
    import numpy as np

    for name, pw in schema.piecewise.items():
        if not pw.convex:
            continue
        try:  # only convex curvature checks need xarray (broadcast over dims)
            import xarray as xr
        except ImportError as exc:
            msg = (
                f"piecewise '{name}': convex curvature validation currently "
                f'requires xarray — pip install "lpspec[linopy]" '
                f'(see issue #27: make this check numpy-only)'
            )
            raise ModuleNotFoundError(msg) from exc
        ctx = f"piecewise '{name}'"
        (x_link, y_link) = pw.links  # convex requires exactly two links
        try:
            xa = _as_dataarray(schema, x_link[1], values)
            ya = _as_dataarray(schema, y_link[1], values)
        except KeyError:
            continue
        xa, ya = xr.broadcast(xa, ya)
        other = [d for d in xa.dims if d != pw.over]
        stacked_x = xa.transpose(*other, pw.over).values.reshape(-1, xa.sizes[pw.over])
        stacked_y = ya.transpose(*other, pw.over).values.reshape(-1, ya.sizes[pw.over])
        for xs, ys in zip(stacked_x, stacked_y, strict=False):
            dx = np.diff(xs)
            if not (dx > 0).all():
                raise PiecewiseExpansionError(
                    f"{ctx}: convex: true requires strictly increasing breakpoints in '{x_link[1]}' (got {xs.tolist()})"
                )
            curvature = np.diff(np.diff(ys) / dx)
            tol = 1e-9 * max(1.0, float(np.abs(ys).max()))
            if (curvature > tol).any() and (curvature < -tol).any():
                raise PiecewiseExpansionError(
                    f'{ctx}: convex: true is not exact for the mixed-curvature '
                    f"curve in '{y_link[1]}' — the hull relaxation would silently "
                    f'cut corners; drop convex: true to use the exact MILP form'
                )


def _as_dataarray(schema: MathSchema, pname: str, values: Mapping[str, Any] | Any) -> Any:
    """One source as a DataArray indexed by its declared dims.

    Two shapes reach here: the linopy lane hands over its ``xr.Dataset``
    entries directly, and the relational lane hands over the tidy frames
    :func:`tidy_sources` normalised. The hop out costs no dependency the
    caller has not taken — asking for a curvature check already requires
    xarray, which brings pandas — but the check still wants to be numpy-only
    (issue #27), which would retire this function.
    """
    import xarray as xr

    if pname not in values:
        raise KeyError(pname)
    obj = values[pname]
    if isinstance(obj, xr.DataArray):
        return obj
    dims = list(schema.parameters[pname].dims)
    frame = as_frame(obj, tuple(dims))
    if frame is None or not dims or 'value' not in frame.collect_schema().names():
        raise KeyError(pname)  # a parquet path, or nothing to lay out: skip
    import pandas as pd

    # column by column through numpy: a whole-frame conversion would reach for
    # pyarrow, and this check already costs the caller xarray without adding a
    # third library to it
    tidy = frame.select([*dims, 'value']).collect()
    columns = {name: tidy[name].to_numpy() for name in tidy.columns}
    return xr.DataArray.from_series(pd.DataFrame(columns).set_index(dims)['value'])
