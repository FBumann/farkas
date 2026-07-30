"""Is the bound data usable? One place, for this lane.

The split this module exists to make explicit (#351):

- **spec validation** — `lpspec/validation.py` — everything decidable from the
  file alone: names, kinds, dim algebra, degree, the closed schema. Law 2 says
  it happens without data, and `check()` is where it happens.
- **data validation** — here — everything that needs the binding: is it there,
  can it be read, is it single-valued per coordinate, are its labels real. Plus
  the two positions where law 8 grants no default (a divisor, a bound), which
  stay with the assembly because they need the matrix.

Every function here is a pure question over frames and declarations, holding no
executor state. That is the point: the executor orchestrates and owns the model
frames, and what counts as *usable data* is decided here, once, where it can be
read without following the build.

**Scoped to this lane on purpose.** These take tidy polars frames. The eager
lane reads pandas/xarray natively because that is what linopy wants, so it keeps
its own checks in `linopy/loader.py` rather than adapting to tidy frames first —
which would cost a copy of every parameter on the lane whose whole point is that
the arrays are already in memory. What the two lanes share instead is the
*wording* (`lpspec/errors.py`) and the *contract*: `tests/test_data_parity.py`
asserts they reach the same verdict on the same bad data. That table is what
keeps this duplication honest.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

import polars as pl

from lpspec.errors import DataError, duplicate_coordinate_message, unknown_labels_message

if TYPE_CHECKING:
    from lpspec.relational import plan

#: The dimension frames a check reads labels out of, by dimension name. Only the
#: ones already built: a dimension derived *from* the parameters is not here when
#: a parameter is checked, and has nothing to answer.
Dimensions = Mapping[str, pl.LazyFrame]


def check_one_row_per_coordinate(p: plan.ParameterDeclaration, frame: pl.LazyFrame, dimensions: Dimensions) -> None:
    """A parameter is a function of its dims: one row per coordinate.

    Two rows for one has no defined meaning, and the eager lane refuses to lay
    such a source out at all, so naming it beats silently summing it. It also
    earns the assembly's skipped aggregate
    (:attr:`~lpspec.relational.compiler.TermFragment.keyed`), for one pass over
    a source orders of magnitude smaller than the matrix.

    A parameter with no dims has exactly one coordinate — the empty one — so the
    same rule reads as "exactly one row", and it is the case where breaking it
    is least visible: a dimensionless parameter is broadcast by joining on
    nothing, which is correct for one row and a silent row multiplication for
    two. In a bound that means duplicate columns for one variable, in a
    where-mask duplicate mask rows, and `keyed` above is claiming the opposite
    of what the source holds (#166).

    The unknown-label question rides along in the same ``select`` rather than
    taking a pass of its own (#350): running it separately cost 0.056 s → 0.151 s
    of build on ``dispatch`` at 1M variables.
    """
    if not p.dims:
        rows = frame.select(pl.len()).collect().item()
        if rows != 1:
            raise DataError(
                f"parameter '{p.name}' is declared with no dims, which means one value "
                f'broadcast everywhere — but its source has {rows} rows. '
                f'Declare the dims it is indexed by, or reduce the source to a single row.'
            )
        return

    # Ask the cheap question first. Naming the offending coordinates needs a
    # group-by, which on a parameter spanning the variable product is the single
    # most expensive thing in the build — 0.88 s of `profiled`'s 2.34 s at 12M
    # rows. Whether *any* coordinate repeats is one pass at 0.25 s, and the
    # group-by is then only paid on the path that is about to raise anyway.
    probes = {'duplicated': pl.struct(p.dims).is_duplicated().any(), **_unknown_label_probes(p, dimensions)}
    answers = frame.select(**probes).collect().row(0, named=True)
    for key, all_known in answers.items():
        if key != 'duplicated' and all_known is False:
            _raise_unknown_label(p, frame, key.removeprefix('known '), dimensions)
    if not answers['duplicated']:
        return

    duplicated = frame.group_by(p.dims).agg(pl.len().alias('n')).filter(pl.col('n') > 1).head(3).collect()
    if duplicated.height == 0:
        return
    shown = '; '.join(
        ', '.join(f'{d}={row[d]!r}' for d in p.dims) + f' ({row["n"]} rows)' for row in duplicated.iter_rows(named=True)
    )
    raise DataError(duplicate_coordinate_message(p.name, shown, list(p.dims)))


def _unknown_label_probes(p: plan.ParameterDeclaration, dimensions: Dimensions) -> dict[str, pl.Expr]:
    """One boolean per dim: are this parameter's labels all coordinates of it?

    Only for dimensions already built — those are the ones with an index of
    their own. A dimension derived from the parameters is not here yet, and
    would have nothing to answer: the union of what arrived is its definition
    (#350).
    """
    probes: dict[str, pl.Expr] = {}
    for d in p.dims:
        if d in dimensions:
            known = dimensions[d].select('val').collect()['val']
            # `.implode()`: `is_in` against a bare Series of the same dtype is
            # ambiguous and deprecated in polars — imploding says "this whole
            # collection", not "element-wise against a list column".
            probes[f'known {d}'] = pl.col(d).is_in(known.implode()).all()
    return probes


def _raise_unknown_label(p: plan.ParameterDeclaration, frame: pl.LazyFrame, d: str, dimensions: Dimensions) -> None:
    """Name the offending labels. Only reached on the path about to raise, so
    the filter and unique it costs are not paid by a healthy build."""
    known = dimensions[d].select('val').collect()['val']
    strangers = frame.filter(~pl.col(d).is_in(known.implode())).select(pl.col(d).unique()).collect()[d].to_list()
    raise DataError(unknown_labels_message(p.name, d, strangers, known.to_list()))


#: Prefix for the scratch count column one coordinate contributes to the
#: caller's aggregate. The spaces make it unrepresentable as a declared name.
NUNIQUE = '__n unique '


def nunique_exprs(names: list[str]) -> list[pl.Expr]:
    """The count columns :func: reads.

    Handed to the caller so they ride in the group_by(d) it already runs
    (#273). Asking separately meant one group_by *per coordinate*, and since
    the frame is a scan over the caller's source, each of those re-opened and
    re-parsed the file.
    """
    return [pl.col(c).n_unique().alias(f'{NUNIQUE}{c}') for c in names]


def check_coordinates_single_valued(d: str, names: list[str], counts: pl.DataFrame) -> None:
    """One label, one coordinate value — two rows disagreeing is a data bug.

    Takes the caller's already-grouped frame rather than the source, so this
    costs no pass of its own. It also names *every* offending coordinate: the
    per-coordinate loop this replaced raised on the first and left the rest to
    be found one build at a time.
    """
    offenders = {c: int((counts[f'{NUNIQUE}{c}'] > 1).sum()) for c in names}
    bad = {c: n for c, n in offenders.items() if n}
    if not bad:
        return
    listed = '; '.join(f"'{c}' ({n} label(s))" for c, n in sorted(bad.items()))
    raise DataError(
        f"dimension '{d}' carries more than one value per label for coordinate(s): "
        f'{listed}. A coordinate is single-valued per label — reduce the source to '
        f'one row per {d}, or model the relation as a parameter instead.'
    )


def check_coordinate_containment(d: str, cname: str, target: str, dimensions: Dimensions) -> None:
    """Every coordinate value must be a label of the dimension it targets.

    A *null* value is not a violation: it says the label belongs to no group,
    which is the same row-absence idiom the rest of the engine uses for "not
    present". Only a value that is present and unknown is a typo, and that is
    the case worth stopping — it would drop terms silently.
    """
    known = dimensions[target].select(pl.col('val').alias(cname))
    bad = (
        dimensions[d]
        .select(cname)
        .filter(pl.col(cname).is_not_null())
        .join(known, on=cname, how='anti')
        .unique()
        .head(5)
        .collect()
    )
    if bad.height == 0:
        return
    shown = ', '.join(repr(v) for v in bad[cname].to_list())
    raise DataError(
        f"dimension '{d}' coordinate '{cname}' has value(s) that are not "
        f"'{target}' coordinates: {shown}. Every value must be a declared "
        f"'{target}' label — otherwise group_sum(over={d}, by={cname}) drops "
        f'those terms in the join that places them, and the model builds and '
        f'solves without them.'
    )
