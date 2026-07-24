"""Built-in helper functions and custom helper registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

# Global registry of helper functions
_REGISTRY: dict[str, Callable[..., Any]] = {}

BUILTIN_NAMES = frozenset({'sum', 'roll', 'shift', 'group_sum'})


def register(name: str) -> Callable:
    """Decorator to register a custom helper function.

    Must be called before ``Model.from_yaml()``.

    Example::

        @linopy_yaml.register('weighted_sum')
        def weighted_sum(array, weights, *, over):
            return (array * weights).sum(over)
    """
    if name in BUILTIN_NAMES:
        msg = f"Cannot register '{name}': conflicts with built-in helper. Built-ins: {sorted(BUILTIN_NAMES)}"
        raise ValueError(msg)

    def decorator(fn: Callable) -> Callable:
        _REGISTRY[name] = fn
        return fn

    return decorator


def get_helper(name: str) -> Callable:
    """Look up a helper function by name.

    Checks built-ins first, then the custom registry.

    Raises
    ------
    NameError
        If the helper is not found.
    """
    if name == 'sum':
        return _helper_sum
    if name == 'roll':
        return _helper_roll
    if name == 'shift':
        return _helper_shift
    if name == 'group_sum':
        return _helper_group_sum
    if name in _REGISTRY:
        return _REGISTRY[name]
    available = sorted(BUILTIN_NAMES | set(_REGISTRY))
    msg = (
        f"Unknown helper function '{name}'.\n"
        f'Available: {available}\n'
        f"Register custom helpers with @linopy_yaml.register('{name}')."
    )
    raise NameError(msg)


def _helper_sum(array: Any, *, over: str) -> Any:
    """Sum *array* over dimension *over*.

    Works with xr.DataArray, linopy Variable, and LinearExpression.
    If the array does not have the named dimension, it is returned unchanged.
    """
    import xarray as xr

    if isinstance(array, xr.DataArray):
        if over in array.dims:
            return array.sum(dim=over)
        return array
    # linopy Variable / LinearExpression
    if hasattr(array, 'dims') and over in array.dims:
        return array.sum(over)
    return array


def _helper_group_sum(array: Any, mapping: Any, *, into: str) -> Any:
    """Sum *array* through a mapping parameter, producing dimension *into*.

    Usage in YAML: ``group_sum(p, gen_bus, into=bus)``

    *mapping* must be a one-dimensional parameter whose values are group
    labels (e.g. ``gen_bus``: generator → bus). The mapping's dimension is
    summed out; a new dimension named *into* holds the group labels.
    """
    import xarray as xr

    if not isinstance(mapping, xr.DataArray):
        msg = (
            f'group_sum() mapping must be a parameter (got '
            f'{type(mapping).__name__}). Usage: group_sum(expr, mapping, into=dim)'
        )
        raise TypeError(msg)
    if mapping.ndim != 1:
        msg = f'group_sum() mapping must have exactly one dimension, got {list(mapping.dims)}'
        raise ValueError(msg)

    group = mapping.rename(into)
    if isinstance(array, xr.DataArray):
        return array.groupby(group).sum()
    if hasattr(array, 'groupby'):  # linopy Variable / LinearExpression
        return array.groupby(group).sum()
    type_name = type(array).__name__
    msg = f"group_sum() does not support type '{type_name}'."
    raise TypeError(msg)


def _helper_shift(array: Any, **kwargs: int) -> Any:
    """Non-cyclic shift along a dimension; vacated positions contribute zero.

    Usage in YAML: ``shift(soc, snapshot=1)`` — the value at *t-1*, with the
    first position empty (an acyclic recurrence, e.g. storage starting empty).
    """
    import xarray as xr

    if len(kwargs) != 1:
        msg = f'shift() expects exactly one keyword argument (dim=n), got {len(kwargs)}: {kwargs}'
        raise TypeError(msg)

    dim, n = next(iter(kwargs.items()))
    if int(n) != n:
        msg = f'shift() amount must be an integer, got {n!r}'
        raise TypeError(msg)
    n = int(n)

    if isinstance(array, xr.DataArray):
        return array.shift({dim: n}, fill_value=0)

    if hasattr(array, 'shift'):  # linopy Variable / LinearExpression
        return array.shift({dim: n})

    type_name = type(array).__name__
    msg = f"shift() does not support type '{type_name}'."
    raise TypeError(msg)


def _helper_roll(array: Any, **kwargs: int) -> Any:
    """Roll (circular shift) *array* along a dimension.

    Usage in YAML: ``roll(soc, snapshot=1)``

    Parameters
    ----------
    array : xr.DataArray
        The array to shift.
    **kwargs : int
        Exactly one keyword argument: ``dim_name=shift_amount``.
    """
    import xarray as xr

    if len(kwargs) != 1:
        msg = f'roll() expects exactly one keyword argument (dim=n), got {len(kwargs)}: {kwargs}'
        raise TypeError(msg)

    dim, shift = next(iter(kwargs.items()))
    if int(shift) != shift:
        msg = f'roll() amount must be an integer, got {shift!r}'
        raise TypeError(msg)
    shift = int(shift)

    if isinstance(array, xr.DataArray):
        return array.roll({dim: shift}, roll_coords=False)

    if hasattr(array, 'roll'):
        return array.roll({dim: shift})

    type_name = type(array).__name__
    msg = f"roll() does not support type '{type_name}'."
    raise TypeError(msg)
