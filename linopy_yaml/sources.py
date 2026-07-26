"""Bind runtime data to a validated schema.

The language says what a parameter *is* — its dims, its dtype — and never
where its values come from. This module is the other half: it takes what the
caller actually passed (parquet paths, or any Arrow-compatible table) and
produces the tidy tables the engine reads by name.

It lives here rather than in ``lowering.py`` because it is not lowering.
Lowering turns an AST into a plan and touches no data at all; this touches
only data and knows nothing about expressions. They were in one file because
``api.build`` calls them on consecutive lines, which is not a reason.

The shapes themselves are recognised in :mod:`linopy_yaml.relational.arrow`,
so no dataframe library is a dependency of either lane.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from linopy_yaml.errors import DataError
from linopy_yaml.piecewise import validate_piecewise_data
from linopy_yaml.relational.arrow import as_table, labels_table

if TYPE_CHECKING:
    from linopy_yaml.schema import MathSchema


def tidy_sources(
    schema: MathSchema,
    data: dict[str, object],
    coords: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Adapt the caller's ``data=``/``coords=`` inputs to executor sources.

    Every in-memory source becomes a tidy :class:`pyarrow.Table` with columns
    ``(dims…, value)``; parquet paths pass through untouched for duckdb to read
    directly. Dimension indexes come from ``data``, ``coords``, declared YAML
    values, or fall back to the executor's inference from parameter tables.

    Normalising here rather than at the executor is what lets the piecewise
    curvature guard see every in-memory shape alike (:mod:`relational.arrow`
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
        table = as_table(obj, pdef.dims)
        if table is None:
            raise DataError(
                f"parameter '{pname}': cannot adapt {type(obj).__name__} to a tidy "
                f'table — pass any Arrow-compatible table with columns '
                f'{[*pdef.dims, "value"]} (pyarrow, polars, pandas), or a parquet path'
            )
        if any(d not in table.column_names for d in pdef.dims):
            raise DataError(
                f"parameter '{pname}': source columns {table.column_names} "
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
        table = as_table(src, (dname,))
        sources[dname] = table if table is not None else labels_table(dname, src)

    validate_piecewise_data(schema, sources)

    return sources
