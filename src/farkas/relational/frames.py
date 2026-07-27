"""The one place that knows what a caller's table library is.

A frame is the lane's boundary (ARCHITECTURE.md, hard rule 2): sources arrive
as a :class:`polars.LazyFrame` or a parquet path, and results leave as a
:class:`polars.DataFrame`. What a caller actually hands over — a polars frame,
a pyarrow table, a pandas frame — is learned from the Arrow PyCapsule protocol,
never from an import, which is why supporting one more of them costs nothing
here and why neither pyarrow nor pandas is a dependency.

Two shapes cannot describe themselves through a capsule, because their
dimensions live in an *index* rather than in columns: ``pandas.Series`` and
``xarray.DataArray``. Those are unwrapped first, and only when their library is
already in ``sys.modules`` — passing one is proof the caller imported it, so
this module still imports neither.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from farkas.errors import DataError

if TYPE_CHECKING:
    from collections.abc import Sequence

    import polars as pl

__all__ = ['as_frame', 'labels_frame']


def as_frame(obj: object, dims: Sequence[str] = ()) -> pl.LazyFrame | None:
    """Normalise one in-memory source to a tidy lazy frame, or ``None``.

    ``None`` means "not table-shaped" and leaves the error message to the
    caller, which knows whether it was holding a parameter or an index.
    *dims* names the columns an index-carrying object's index becomes.
    """
    import sys

    import polars as pl

    # a bool is an int, so it is caught first — and kept boolean rather than
    # widened to a float. A bool parameter is a mask, and the executor reads
    # its truthiness from the column *type*: as a float it would silently fall
    # back to the present-and-finite rule instead (#47).
    if isinstance(obj, bool) and not dims:
        return pl.LazyFrame({'value': [obj]}, schema={'value': pl.Boolean})
    if isinstance(obj, (int, float)) and not isinstance(obj, bool) and not dims:
        return pl.LazyFrame({'value': [float(obj)]}, schema={'value': pl.Float64})
    if isinstance(obj, pl.LazyFrame):
        return obj
    if isinstance(obj, pl.DataFrame):
        return obj.lazy()

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
    if pd is not None and isinstance(obj, pd.DataFrame):
        return _from_pandas(obj)

    if hasattr(obj, '__arrow_c_stream__') or hasattr(obj, '__arrow_c_array__'):
        try:
            # the capsule is the whole contract: polars reads it without this
            # module knowing which library produced it
            return pl.DataFrame(obj).lazy()  # pyrefly: ignore[bad-argument-type]  — narrowed by the capsule test
        except (TypeError, ValueError, pl.exceptions.PolarsError):
            return None  # a capsule describing a bare array, not a table
    return None


def _from_pandas(frame: Any) -> pl.LazyFrame:
    """A pandas frame, column by column, without reaching for pyarrow.

    A whole-frame conversion asks polars to read pandas' memory directly, and
    on anything Arrow-backed — which strings are by default on pandas 3 — that
    means pyarrow. Going through numpy keeps the bridge to the two libraries
    already present.

    Object arrays are the case worth handling explicitly: a *partial*
    coordinate is a string column with gaps, and numpy renders those gaps as
    float ``nan``, which is not a missing string. Passing the values as a list
    lets polars read them as nulls, which is what row absence means here. The
    test is on the *numpy* dtype rather than the pandas one, because which
    pandas dtypes land as objects has changed across releases and the array is
    what polars actually sees.
    """
    import polars as pl

    columns: dict[str, Any] = {}
    for name in frame.columns:
        values = frame[name].to_numpy()
        if values.dtype == object:
            columns[name] = pl.Series(name, [None if _is_missing(v) else v for v in values], strict=False)
        else:
            columns[name] = values
    return pl.DataFrame(columns).lazy()


def _is_missing(value: Any) -> bool:
    """Whether an object-array entry is pandas' rendering of "no value"."""
    return value is None or (isinstance(value, float) and value != value)


def labels_frame(dname: str, values: object) -> pl.LazyFrame:
    """A one-column index frame from a plain sequence of labels."""
    import polars as pl

    try:
        labels: list[Any] = list(values)  # pyrefly: ignore[bad-argument-type]  — `values` is whatever a caller passed
        return pl.LazyFrame({dname: labels})
    except (TypeError, pl.exceptions.PolarsError) as exc:
        raise DataError(
            f"index for dimension '{dname}': cannot read labels out of "
            f'{type(values).__name__} — pass a sequence of labels, a table '
            f'polars can read with a {dname!r} column, or a parquet path'
        ) from exc
