"""The one place that knows what a caller's table library is.

Arrow is the lane's boundary (ARCHITECTURE.md, hard rule 2): sources arrive as
``pyarrow.Table`` or a parquet path, and results leave as ``pyarrow.Table``, so
no dataframe library is a dependency of the engine. What a caller actually
hands over — a polars frame, a pandas frame, a pyarrow table — is learned from
the Arrow PyCapsule protocol, never from an import, which is why supporting one
more of them costs nothing here.

Two shapes cannot describe themselves through a capsule, because their
dimensions live in an *index* rather than in columns: ``pandas.Series`` and
``xarray.DataArray``. Those are unwrapped first, and only when their library is
already in ``sys.modules`` — passing one is proof the caller imported it, so
this module still imports neither.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from linopy_yaml.errors import DataError

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ['as_table', 'labels_table']


def as_table(obj: object, dims: Sequence[str] = ()) -> Any | None:
    """Normalise one in-memory source to a tidy ``pyarrow.Table``, or ``None``.

    ``None`` means "not table-shaped" and leaves the error message to the
    caller, which knows whether it was holding a parameter or an index.
    *dims* names the columns an index-carrying object's index becomes.
    """
    import sys

    import pyarrow as pa

    # a bool is an int, so it is caught first — and kept boolean rather than
    # widened to a float. A bool parameter is a mask, and the executor reads
    # its truthiness from the column *type*: as a float it would silently fall
    # back to the present-and-finite rule instead (#47).
    if isinstance(obj, bool) and not dims:
        return pa.table({'value': pa.array([obj], type=pa.bool_())})
    if isinstance(obj, (int, float)) and not isinstance(obj, bool) and not dims:
        return pa.table({'value': pa.array([float(obj)], type=pa.float64())})
    if isinstance(obj, pa.Table):
        return obj
    if isinstance(obj, pa.RecordBatch):
        return pa.Table.from_batches([obj])

    xr = sys.modules.get('xarray')
    if xr is not None and isinstance(obj, xr.DataArray):
        obj = obj.to_series()
    pd = sys.modules.get('pandas')
    if pd is not None and isinstance(obj, pd.Series):
        # a Series exposes a capsule too, but it describes the values alone —
        # the index, which holds the dims, has to be promoted to columns first.
        # Levels the caller named are left alone and bind by name: overwriting
        # them with *dims* transposes the data silently when two dims share a
        # label space, and nothing downstream can catch that.
        if any(n is None for n in obj.index.names):
            obj = obj.rename_axis(dims)
        obj = obj.rename('value').reset_index()

    if hasattr(obj, '__arrow_c_stream__'):
        try:
            return pa.table(obj)
        except pa.ArrowInvalid:
            return None  # a capsule describing a bare array, not a table
    return None


def labels_table(dname: str, values: object) -> Any:
    """A one-column index table from a plain sequence of labels."""
    import pyarrow as pa

    try:
        return pa.table({dname: pa.array(list(values))})  # pyrefly: ignore[bad-argument-type]
    except (TypeError, pa.ArrowInvalid) as exc:
        raise DataError(
            f"index for dimension '{dname}': cannot read labels out of "
            f'{type(values).__name__} — pass a sequence of labels, an '
            f'Arrow-compatible table with a {dname!r} column, or a parquet path'
        ) from exc
