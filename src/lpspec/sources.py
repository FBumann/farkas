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
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from lpspec.errors import DataError
from lpspec.piecewise import validate_piecewise_data
from lpspec.relational.frames import as_frame, labels_frame

if TYPE_CHECKING:
    from lpspec.schema import MathSchema


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
