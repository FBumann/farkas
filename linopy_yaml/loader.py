"""Data loading, coercion, and validation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import numpy as np
import pandas as pd
import xarray as xr

from linopy_yaml.errors import DataError

if TYPE_CHECKING:
    from linopy_yaml.schema import MathSchema


def build_master_coords(
    schema: MathSchema,
    coords: dict[str, Any] | None,
) -> dict[str, pd.Index]:
    """Assemble master coordinate indices for every declared dimension.

    Sources in order of precedence:
    1. ``coords`` kwarg (highest priority).
    2. ``values`` declared in the YAML.
    3. If neither → raise immediately.
    """
    coords = coords or {}
    master: dict[str, pd.Index] = {}

    for dim_name, dim_def in schema.dimensions.items():
        if dim_name in coords:
            master[dim_name] = dim_index_of(coords[dim_name], dim_name)
        elif dim_def.values is not None:
            master[dim_name] = pd.Index(dim_def.values, name=dim_name)
        else:
            msg = (
                f"Dimension '{dim_name}' has no values.\n"
                f"Declare them under 'dimensions.{dim_name}.values' in the YAML\n"
                f"or pass coords={{'{dim_name}': [...]}}."
            )
            raise DataError(msg)

    return master


def dim_index_of(source: Any, dim_name: str) -> pd.Index:
    """A dimension index from a label sequence or a frame carrying coordinates."""
    if isinstance(source, pd.DataFrame):
        if dim_name not in source.columns:
            msg = (
                f"coords['{dim_name}'] is a DataFrame without a '{dim_name}' column "
                f'(has {list(source.columns)}). The label column must be named after '
                f'the dimension.'
            )
            raise DataError(msg)
        return pd.Index(pd.unique(source[dim_name]), name=dim_name)
    return pd.Index(source, name=dim_name)


def build_dim_coords(
    schema: MathSchema,
    coords: dict[str, Any] | None,
    master_coords: dict[str, pd.Index],
) -> dict[str, dict[str, xr.DataArray]]:
    """Assemble declared coordinates, checked against the dimension they target.

    A coordinate is a column of the dimension's index source, so it arrives
    through ``coords=`` as a DataFrame carrying the label column plus one
    column per declared coordinate. The containment check mirrors the
    relational lane's: a value that is not a label of the target dimension
    would otherwise be dropped by xarray's inner-join alignment, silently
    losing the term it carries.
    """
    coords = coords or {}
    out: dict[str, dict[str, xr.DataArray]] = {}

    for dim_name, dim_def in schema.dimensions.items():
        if not dim_def.coords:
            continue
        source = coords.get(dim_name)
        if not isinstance(source, pd.DataFrame):
            got = type(source).__name__ if source is not None else 'nothing'
            msg = (
                f"Dimension '{dim_name}' declares coordinates {sorted(dim_def.coords)}, "
                f"so coords['{dim_name}'] must be a DataFrame with a '{dim_name}' column "
                f'plus one column per coordinate (got {got}).'
            )
            raise DataError(msg)
        missing = [c for c in sorted(dim_def.coords) if c not in source.columns]
        if missing:
            msg = (
                f"Dimension '{dim_name}' index is missing declared coordinate column(s) "
                f'{missing} (has {list(source.columns)}).'
            )
            raise DataError(msg)

        labels = master_coords[dim_name]
        first = source.drop_duplicates(subset=[dim_name]).set_index(dim_name)
        counts = source.groupby(dim_name, sort=False)[sorted(dim_def.coords)].nunique()
        out[dim_name] = {}
        for cname, target in dim_def.coords.items():
            if (counts[cname] > 1).any():
                offending = sorted(counts.index[counts[cname] > 1].astype(str))[:5]
                msg = (
                    f"Dimension '{dim_name}': label(s) {offending} carry more than one "
                    f"value for coordinate '{cname}'. A coordinate is single-valued per "
                    f'label.'
                )
                raise DataError(msg)
            series = first[cname].reindex(labels)
            known = set(master_coords[target])
            unknown = sorted({str(v) for v in series if v not in known})[:5]
            if unknown:
                msg = (
                    f"Dimension '{dim_name}' coordinate '{cname}' has value(s) that are "
                    f"not '{target}' coordinates: {', '.join(unknown)}. Every value must "
                    f"be a declared '{target}' label — otherwise "
                    f'group_sum(over={dim_name}, by={cname}) drops those terms and the '
                    f'model builds and solves without them.'
                )
                raise DataError(msg)
            out[dim_name][cname] = xr.DataArray(
                series.to_numpy(),
                dims=[dim_name],
                coords={dim_name: labels},
                name=cname,
            )

    return out


def load_parameters(
    schema: MathSchema,
    data: dict[str, Any] | None,
    master_coords: dict[str, pd.Index],
) -> xr.Dataset:
    """Load, coerce, and validate all declared parameters.

    Returns an ``xr.Dataset`` with one DataArray per parameter, aligned to
    the master coordinates.
    """
    data = data or {}
    arrays: dict[str, xr.DataArray] = {}

    # Step 2: check all declared parameters are provided
    for pname in schema.parameters:
        if pname not in data:
            msg = f"Parameter '{pname}' is required but was not provided in data.\nAdd '{pname}' to the data= argument."
            raise DataError(msg)

    # Step 5: reject unknown data keys
    declared = set(schema.parameters)
    unknown = set(data) - declared
    if unknown:
        msg = (
            f'The following data keys are not declared as parameters: '
            f'{sorted(unknown)}.\n'
            f"Declare them under 'parameters:' in the YAML or remove "
            f'them from data=.'
        )
        raise DataError(msg)

    # Coerce each parameter
    for pname, pdef in schema.parameters.items():
        raw = data[pname]
        arr = _coerce_to_dataarray(pname, raw, pdef.dims, master_coords)

        # Expand scalars and reindex to master coords
        if pdef.dims:
            reindex_coords = {d: master_coords[d] for d in pdef.dims}
            if arr.ndim == 0:
                # Broadcast scalar to full shape over declared dims
                scalar_val = float(arr.values)
                shape = tuple(len(master_coords[d]) for d in pdef.dims)
                arr = xr.DataArray(
                    np.full(shape, scalar_val),
                    dims=pdef.dims,
                    coords=reindex_coords,
                )
            else:
                arr = arr.reindex(reindex_coords)

        arrays[pname] = arr

    return xr.Dataset(arrays)


def _coerce_to_dataarray(
    name: str,
    raw: Any,
    dims: list[str],
    master_coords: dict[str, pd.Index],
) -> xr.DataArray:
    """Coerce a user-provided value into an xr.DataArray."""
    # Scalar
    if isinstance(raw, (int, float, np.integer, np.floating)):
        return xr.DataArray(float(raw))

    # dict → pd.Series → DataArray
    if isinstance(raw, dict):
        if len(dims) != 1:
            msg = f"Parameter '{name}': dict input is only supported for 1-D parameters, but declared dims are {dims}."
            raise DataError(msg)
        series = pd.Series(raw)
        series.index.name = dims[0]
        raw = series  # fall through to Series handling

    # pd.Series
    if isinstance(raw, pd.Series):
        if len(dims) != 1:
            msg = (
                f"Parameter '{name}': pd.Series input is only supported for "
                f'1-D parameters, but declared dims are {dims}.'
            )
            raise DataError(msg)
        if raw.index.name is None:
            raw = raw.copy()
            raw.index.name = dims[0]
        arr = xr.DataArray.from_series(raw)
        _validate_dims(name, arr, dims)
        _validate_coords(name, arr, master_coords)
        return arr

    # pd.DataFrame
    if isinstance(raw, pd.DataFrame):
        if len(dims) != 2:
            msg = (
                f"Parameter '{name}': pd.DataFrame input is only supported for "
                f'2-D parameters, but declared dims are {dims}.'
            )
            raise DataError(msg)
        if raw.index.name is None:
            raw = raw.copy()
            raw.index.name = dims[0]
        if raw.columns.name is None:
            raw.columns.name = dims[1]
        # flat columns (checked above: exactly 2 dims), so stack() gives a Series
        stacked = cast('pd.Series', raw.stack())
        stacked.name = name
        arr = xr.DataArray.from_series(stacked).unstack()
        _validate_dims(name, arr, dims)
        _validate_coords(name, arr, master_coords)
        return arr

    # xr.DataArray
    if isinstance(raw, xr.DataArray):
        _validate_dims(name, raw, dims)
        _validate_coords(name, raw, master_coords)
        return raw

    # np.ndarray / list
    if isinstance(raw, (np.ndarray, list)):
        arr_np = np.asarray(raw)
        if arr_np.ndim == 0:
            return xr.DataArray(float(arr_np))
        if arr_np.ndim == 1 and len(dims) == 1:
            dim = dims[0]
            if len(arr_np) != len(master_coords[dim]):
                msg = (
                    f"Parameter '{name}': array length {len(arr_np)} does not "
                    f"match master coordinate '{dim}' length "
                    f'{len(master_coords[dim])}.'
                )
                raise DataError(msg)
            return xr.DataArray(arr_np, dims=[dim], coords={dim: master_coords[dim]})
        msg = (
            f"Parameter '{name}': unsupported type ndarray.\n"
            f'For multi-dimensional arrays without named axes, provide a '
            f'pandas DataFrame or xr.DataArray with named dimensions.\n'
            f'Declared dims: {dims}.'
        )
        raise DataError(msg)

    type_name = type(raw).__name__
    msg = f"Parameter '{name}': unsupported type '{type_name}'."
    raise TypeError(msg)


def _validate_dims(
    name: str,
    arr: xr.DataArray,
    declared_dims: list[str],
) -> None:
    """Check that the DataArray's dims are a subset of declared dims."""
    unexpected = set(arr.dims) - set(declared_dims)
    if unexpected:
        msg = (
            f"Parameter '{name}' has unexpected dimensions {unexpected}.\n"
            f'Declared dims: {declared_dims}.\n'
            f'Either update the declaration or reshape your data.'
        )
        raise DataError(msg)


def _validate_coords(
    name: str,
    arr: xr.DataArray,
    master_coords: dict[str, pd.Index],
) -> None:
    """Check that all coordinate values exist in the master coordinate."""
    for dim in arr.dims:
        if dim not in master_coords:
            continue
        arr_vals = set(arr.coords[dim].values)
        master_vals = set(master_coords[dim])
        unknown = arr_vals - master_vals
        if unknown:
            msg = (
                f"Parameter '{name}' has values in dimension '{dim}' "
                f'that are not in the master coordinate: {sorted(unknown)}.\n'
                f"Master '{dim}' coords: {list(master_coords[dim])}"
            )
            raise DataError(msg)
