"""Expand ``piecewise:`` blocks into plain variables and constraints.

This is schema-level expansion (SPEC §12.4): a ``piecewise:`` block becomes
ordinary affine declarations *before* anything is built, so both backends —
eager and relational — receive identical schemas and stay differential-
testable. Formulations never enter the plan as expression nodes.

The λ convex-combination method is used because it is expansion-pure: it
needs only the breakpoint coordinate parameters themselves, no derived data
(no slopes, intercepts, or segment lengths). For a block

    piecewise:
      curve:
        over: bp
        links:
          - [power, power_bp]
          - [fuel * eff, fuel_bp, "<="]

with F = the union of the links' dims, it emits:

    variables:
      curve_lam(F, bp)  in [0, 1]
      curve_seg(F, bp)  binary                            (omitted when convex)
    constraints:
      curve_convexity(F):     sum(curve_lam, over=bp) == 1
      curve_pick(F):          sum(curve_seg, over=bp) == 1        (when not convex)
      curve_adjacency(F, bp): curve_lam <= curve_seg + shift(curve_seg, over=bp, by=1, edge=0)
      curve_link0(F):         (power) == sum(curve_lam * power_bp, over=bp)
      curve_link1(F):         (fuel * eff) <= sum(curve_lam * fuel_bp, over=bp)

With adjacency, at most two *neighbouring* λ are nonzero, so the linked
expressions lie on the piecewise curve exactly. Without it (``convex:
true``), they range over the convex hull of the breakpoints — the correct
relaxation for convex/concave curves under optimisation pressure.

Link expressions must use the core language subset (their dims are inferred
through the lowering machinery), which they need anyway to run on both
backends.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lpspec.errors import LanguageError, PiecewiseExpansionError
from lpspec.language.dimensions import dims_of
from lpspec.language.expression_parser import ComparisonNode, parse_expression
from lpspec.language.resolution import Namespace, resolve_expression
from lpspec.language.schema import MathSchema, PiecewiseBlock
from lpspec.lowering import check_core_subset
from lpspec.relational.frames import as_frame

if TYPE_CHECKING:
    from collections.abc import Mapping


def expand_piecewise(schema: MathSchema) -> MathSchema:
    """Return *schema* with every ``piecewise:`` block expanded away."""
    if not schema.piecewise:
        return schema

    raw = schema.model_dump()
    for name, pw in schema.piecewise.items():
        frame = _validate_block(schema, name, pw)
        lam, seg = f'{name}_lam', f'{name}_seg'

        raw['variables'][lam] = {
            'foreach': [*frame, pw.over],
            'bounds': {'lower': 0.0, 'upper': 1.0},
        }
        rhs = f'({pw.active})' if pw.active else '1'
        raw['constraints'][f'{name}_convexity'] = {
            'foreach': list(frame),
            'expression': f'sum({lam}, over={pw.over}) == {rhs}',
        }
        for i, link in enumerate(pw.links):
            expr, values = link[0], link[1]
            sign = link[2] if len(link) == 3 else '=='
            raw['constraints'][f'{name}_link{i}'] = {
                'foreach': list(frame),
                'expression': (f'({expr}) {sign} sum({lam} * {values}, over={pw.over})'),
            }
        if not pw.convex:
            raw['variables'][seg] = {
                'foreach': [*frame, pw.over],
                'binary': True,
                'bounds': {},
            }
            raw['constraints'][f'{name}_pick'] = {
                'foreach': list(frame),
                'expression': f'sum({seg}, over={pw.over}) == {rhs}',
            }
            raw['constraints'][f'{name}_adjacency'] = {
                'foreach': [*frame, pw.over],
                # fill=0 rather than a bare shift: at the first breakpoint the
                # vacated term must contribute zero, giving `lam <= seg`. Left
                # absent it would propagate and drop that row, leaving the first
                # lambda unconstrained by segment selection — a wrong MILP with
                # no error, which is why #289 kept the escape hatch.
                'expression': f'{lam} <= {seg} + shift({seg}, over={pw.over}, by=1, edge=0)',
            }

    raw['piecewise'].clear()  # every block is now expanded away
    return MathSchema(**raw)


def _validate_block(schema: MathSchema, name: str, pw: PiecewiseBlock) -> tuple[str, ...]:
    """Check references and infer the frame (union of the links' dims)."""
    ctx = f"piecewise '{name}'"
    if pw.over not in schema.dimensions:
        raise PiecewiseExpansionError(f"{ctx}: over references undeclared dimension '{pw.over}'")

    frame: list[str] = []
    for i, link in enumerate(pw.links):
        expr_text, values = link[0], link[1]
        if values not in schema.parameters:
            raise PiecewiseExpansionError(f"{ctx}: link {i} values references undeclared parameter '{values}'")
        if pw.over not in schema.parameters[values].dims:
            raise PiecewiseExpansionError(
                f"{ctx}: link {i} values parameter '{values}' must carry dim "
                f"'{pw.over}' (has {schema.parameters[values].dims})"
            )
        for d in _declared_order(schema, _expr_dims(schema, expr_text, f'{ctx} link {i}')):
            if d == pw.over:
                raise PiecewiseExpansionError(
                    f"{ctx}: link {i} expression already carries the breakpoint dim '{pw.over}'"
                )
            if d not in frame:
                frame.append(d)

    if pw.active is not None:
        if pw.active in schema.variables and not schema.variables[pw.active].binary:
            raise PiecewiseExpansionError(f"{ctx}: active variable '{pw.active}' must be binary")
        for d in _declared_order(schema, _expr_dims(schema, pw.active, f'{ctx} active')):
            if d == pw.over:
                raise PiecewiseExpansionError(f"{ctx}: active expression must not carry the breakpoint dim '{pw.over}'")
            if d not in frame:
                frame.append(d)

    # Every name the expansion will emit, checked against what the file already
    # declares. One list per kind rather than one loop per name family: a new
    # emitted declaration is then a name here, not a fourth loop to remember.
    emitted_constraints = (
        f'{name}_convexity',
        f'{name}_pick',
        f'{name}_adjacency',
        *(f'{name}_link{i}' for i in range(len(pw.links))),
    )
    for kind, emitted, declared in (
        ('variable', (f'{name}_lam', f'{name}_seg'), schema.variables),
        ('constraint', emitted_constraints, schema.constraints),
    ):
        for one in emitted:
            if one in declared:
                raise PiecewiseExpansionError(f"{ctx}: emitted {kind} '{one}' collides with a declared {kind}")
    return tuple(frame)


def _declared_order(schema: MathSchema, dims: frozenset[str]) -> list[str]:
    """*dims* in the order the file declares them.

    An emitted ``foreach`` is a *language* object and inherits the label
    contract in ARCHITECTURE — row-major over the coordinate product, the same
    run to run. Iterating the dim *set* instead spends string hashing, which is
    randomised per process, so the emitted order and every solver column index
    behind it varied between builds of the same model. Declaration order is
    what a hand-written ``foreach`` gets, so it is what an emitted one gets.
    """
    declared = [d for d in schema.dimensions if d in dims]
    return declared + sorted(dims.difference(declared))


def _expr_dims(schema: MathSchema, text: str, ctx: str) -> frozenset[str]:
    """Dims of an affine link expression, checked to be in the core subset.

    Lowering is the subset test — it is what raises on a link the engine has
    no plan node for — and ``dimensions`` is the dim set, which is a language
    property rather than a lowering by-product.
    """
    ast = parse_expression(text)
    if isinstance(ast, ComparisonNode):
        raise PiecewiseExpansionError(f'{ctx}: link expressions must not contain a comparison, got {text!r}')
    errors: list[str] = []
    resolved = resolve_expression(ast, Namespace.of(schema), ctx, errors)
    if resolved is None:
        raise PiecewiseExpansionError('\n'.join(errors))
    assert not isinstance(resolved, ComparisonNode)
    try:
        check_core_subset(resolved, schema, ctx)
        return dims_of(resolved, schema, ctx)
    except LanguageError as exc:
        raise PiecewiseExpansionError(
            f'{ctx}: link expression {text!r} is not a core-subset affine expression: {exc}'
        ) from exc


def validate_piecewise_data(schema: MathSchema, values: Mapping[str, Any] | Any) -> None:
    """Data-time guard for ``convex: true`` blocks (SPEC §3.6).

    The hull relaxation is silently wrong for curves of mixed curvature, and
    ill-defined when the x-breakpoints are not strictly monotone — with the
    breakpoint values in hand (which the schema never has), both are
    checkable. *values* maps parameter names to whatever its lane holds: the
    tidy ``pyarrow.Table`` / parquet path of
    :func:`~lpspec.lowering.tidy_sources`, or the linopy lane's
    ``xr.Dataset``. Blocks whose parameters are missing, or bound to a path
    (not readable in process), are skipped; a missing parameter errors
    elsewhere.
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
    :func:`sources.tidy_sources` normalised. The hop out costs no dependency
    the caller has not taken — asking for a curvature check already requires
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
