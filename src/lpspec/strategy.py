"""Solving strategies: one plan per slice, folded.

A plan cannot contain a loop; a *process* may loop over plans, each with its
shape fixed before its own data (docs/design/ceiling.md). So a strategy is
never a language feature and never an engine feature — it is a driver above
:mod:`lpspec.api`, and everything here is built from the three public verbs.

Every strategy is the same fold: **partition → build → solve → carry →
stitch**. What differs is only how slices are cut and whether they couple.

    scenario / sweep    ``EachCoordinate('scenario')``              independent
    myopic pathway      ``EachCoordinate('period', ordered=True)``  + ``carry``
    rolling horizon     ``EachWindow('snapshot', 48, 24, 't')``     + ``carry``

**No language change makes this work**, and that is the point: the seam of a
rolling horizon is an ordinary constraint (``where: "t == 0"`` reading a
carried parameter), the partition is a filter on the sources, and neither lane
learns a new word. Hard rule 3 is untouched.

**A partition is a filter on the sources, not a narrower ``coords``.** Passing
``coords`` alone leaves the parameter rows outside the window in place, and the
containment check refuses them by design — a label that is not a coordinate is
a typo, not an instruction. So an axis rewrites the sources and supplies the
matching ``coords`` together.

Why the axes are dataclasses and the keywords are not: a config object earns a
name when ``__post_init__`` can validate the group *without looking outward*.
:class:`EachWindow` can (``step <= length``, ``into != dim``); a wrapper over
``executor``/``carry``/``keep`` cannot, because the constraints that bite —
``carry`` excludes an executor, and a carry index is read against
``EachWindow.step`` — cross every boundary one could draw. Grouping those would
validate the weakest constraint and hide the two real ones behind objects that
look self-contained.
"""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import polars as pl

from lpspec.api import solve as _solve
from lpspec.errors import DataError, LpspecError
from lpspec.relational.frames import as_frame

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

#: How a frame crosses a process boundary. Parquet rather than pickle, and not
#: a knob: measured over 1M rows, zstd is 8.3x smaller *and* 3x faster than
#: pickling the frame, and still smaller and faster than pickle on
#: incompressible float64. The only trade a knob would expose is snappy over
#: zstd, worth ~10 ms per source and only on links faster than ~300 MB/s.
_COMPRESSION = 'zstd'


# ---------------------------------------------------------------------------
# axes — how slices are cut
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EachCoordinate:
    """One slice per coordinate of *dim* — a column the sources carry.

    Scenarios, draws, investment periods. *dim* names the axis; its coordinates
    are the slice keys. Sources carrying it are filtered to one coordinate and
    the column dropped, so **the model never mentions it**; every other source
    passes through untouched.

    ``ordered`` says the coordinates have a meaningful sequence, which is what
    a ``carry`` needs — scenarios have no "next", investment periods do.
    """

    dim: str
    ordered: bool = False

    @property
    def _key_name(self) -> str:
        return self.dim

    def _is_ordered(self) -> bool:
        return self.ordered

    def _slices(self, sources: Mapping[str, Any]) -> list[tuple[Any, dict[str, Any], dict[str, Any]]]:
        carrying = _carrying(sources, self.dim)
        if not carrying:
            raise DataError(
                f"no source carries a '{self.dim}' column, so there is nothing to slice over. "
                f'EachCoordinate names a column the data has; a span of consecutive coordinates is EachWindow.'
            )
        keys = sorted({k for name in carrying for k in _distinct(sources[name], self.dim)})
        out: list[tuple[Any, dict[str, Any], dict[str, Any]]] = []
        for key in keys:
            sliced = dict(sources)
            for name in carrying:
                sliced[name] = _lazy(sources[name]).filter(pl.col(self.dim) == key).drop(self.dim)
            out.append((key, sliced, {}))
        return out


@dataclass(frozen=True)
class EachWindow:
    """One slice per window of consecutive coordinates of *dim*.

    Unlike :class:`EachCoordinate` the dimension is re-indexed rather than
    dropped, because a window holds many coordinates and the model has to be
    able to name their order.

    ``length`` and ``step`` count **coordinates, not coordinate values**:
    ``length=48`` is forty-eight snapshots whatever they are numbered, and the
    only thing *dim* has to be is **orderable**. Datetimes, strings and gapped
    integers all work; measuring in values instead made dense integers from
    zero the one case that behaved, turned an hourly datetime index into a
    ``TypeError``, and — worse — silently produced 26 mostly-empty slices where
    three were meant on integers spaced ten apart.

    ``length`` is what the solver sees, ``step`` is what you keep, and
    ``length > step`` is overlap — the tail exists to stop end-effects at the
    seam and is discarded by whoever reads the result.

    **``into`` is structural, and has no default.** The seam row of a windowed
    model is ``where: "t == 0"``, which needs a literal; "the first coordinate
    of *this* window" is not one in global numbering, and will not become one
    when indexed access lands. Re-indexing is the mechanism, the local index is
    dense ``0..n-1`` by construction — so that literal always matches — and the
    name belongs to whoever wrote the model, which is why guessing ``t`` here
    would be guessing at somebody else's decision.

    For grouping that is not positional — every calendar month, say, where the
    groups have different sizes — precompute the group column and use
    :class:`EachCoordinate`. What this class uniquely offers is **overlap**,
    which no amount of preprocessing gives the other one.
    """

    dim: str
    length: int
    step: int
    into: str

    def __post_init__(self) -> None:
        if self.length < 1 or self.step < 1:
            raise ValueError(f'length and step must be positive (got length={self.length}, step={self.step})')
        if self.step > self.length:
            raise ValueError(
                f'step={self.step} exceeds length={self.length}, which would skip coordinates between '
                f'windows. step == length is contiguous; step < length overlaps.'
            )
        if not self.into:
            raise ValueError('into must name the local index the model declares — it has no default')
        if self.into == self.dim:
            raise ValueError(f'into={self.into!r} must differ from dim — the local index replaces the global one')

    @property
    def _key_name(self) -> str:
        return self.dim

    def _is_ordered(self) -> bool:
        return True

    def _slices(self, sources: Mapping[str, Any]) -> list[tuple[Any, dict[str, Any], dict[str, Any]]]:
        carrying = _carrying(sources, self.dim)
        if not carrying:
            raise DataError(f"no source carries a '{self.dim}' column, so there is nothing to window over")
        # the ordered coordinates, unioned across every source that carries the
        # dimension — a window is a span of *these*, never of the numbers in them
        coordinates = sorted({c for name in carrying for c in _distinct(sources[name], self.dim)})
        out: list[tuple[Any, dict[str, Any], dict[str, Any]]] = []
        for start in range(0, len(coordinates), self.step):
            window = coordinates[start : start + self.length]
            local = {coordinate: position for position, coordinate in enumerate(window)}
            sliced = dict(sources)
            for name in carrying:
                sliced[name] = (
                    _lazy(sources[name])
                    # the filter is what a scan can push down; the mapping that
                    # follows is over a frame already cut to one window
                    .filter(pl.col(self.dim).is_in(window))
                    .with_columns(pl.col(self.dim).replace_strict(local, return_dtype=pl.Int64).alias(self.into))
                    .drop(self.dim)
                )
            # keyed by the window's first coordinate, not its position: that is
            # what names the window in the caller's own terms
            out.append((window[0], sliced, {self.into: range(len(window))}))
        return out


#: What ``axis=`` accepts. A plain list of ``(key, sources, coords)`` is also
#: taken, so an irregular ladder or a hand-built draw needs no third class.
Axis = EachCoordinate | EachWindow


# ---------------------------------------------------------------------------
# the result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Runs:
    """What a fold returned: frames keyed by slice, never a scalar.

    **There is no aggregate objective**, and that is deliberate. Scenarios are
    a distribution rather than a sum, and summing window objectives
    double-counts whatever the overlap discards. :attr:`objective` is a frame;
    the caller reduces it having said what they mean.

    **Duals are not exposed.** A window's shadow price is that window's, and
    concatenating them into a price curve is wrong in a way nothing complains
    about. Reading them per slice is a deliberate step, not a convenience.
    """

    key_name: str
    meta: pl.DataFrame
    _primals: dict[str, pl.DataFrame] = field(repr=False, default_factory=dict)

    @property
    def objective(self) -> pl.DataFrame:
        """``(key, status, termination_condition, objective)``, in slice order."""
        return self.meta

    @property
    def keys(self) -> list[Any]:
        return self.meta[self.key_name].to_list()

    def primal(self, name: str) -> pl.DataFrame:
        """One variable's values across every slice, the slice key prepended."""
        if name not in self._primals:
            kept = ', '.join(repr(k) for k in sorted(self._primals)) or 'nothing'
            raise LpspecError(
                f'variable {name!r} was not kept — this run kept {kept}. '
                f"Name it in keep=(...) : a fold releases each slice's model as it goes, so what "
                f'is not extracted inside the loop cannot be read afterwards.'
            )
        return self._primals[name]

    def __len__(self) -> int:
        return self.meta.height


# ---------------------------------------------------------------------------
# the fold
# ---------------------------------------------------------------------------


def solve_over(
    model: Any,
    sources: Mapping[str, Any],
    axis: Axis | Sequence[tuple[Any, dict[str, Any], dict[str, Any]]],
    *,
    carry: Mapping[str, tuple[str, int | None]] | None = None,
    keep: Sequence[str] = (),
    executor: Any = None,
    shared_fs: bool = False,
    solver_options: Mapping[str, Any] | None = None,
    solver_name: str = 'highs',
    **build_kwargs: Any,
) -> Runs:
    """Solve *model* once per slice of *axis* and fold the answers together.

    ``carry`` maps a **parameter** to ``(variable, index)`` — the value of that
    variable at that local index in slice *i* becomes the parameter's value in
    slice *i+1*. ``index=None`` means the variable has exactly one row in a
    slice. It is a **copy and never arithmetic**: a myopic pathway accumulates
    (``existing += built``), and the way to say so is a derived variable in the
    YAML, where the math is reviewable and the oracle can see it.

    **The carry index is explicit on purpose.** With ``EachWindow(48, 24)`` the
    state to carry sits at local index 23, not 47 — an implicit "last" would be
    correct until the day overlap is introduced and silently wrong after it.

    ``keep`` names the variables whose primals survive. This is a fold rather
    than a list comprehension: each slice's model is released as the loop goes,
    so peak stays at one slice instead of N.

    ``executor`` is any :class:`concurrent.futures.Executor` — a process pool,
    or anything remote implementing the same protocol. ``None`` is sequential.
    A ``carry`` makes slices sequential by definition, so the two are refused
    together rather than one silently winning.

    **A process pool must not use the ``fork`` start method.** polars' thread
    pool does not survive a fork, and a forked worker hangs rather than failing
    — the worst shape a failure can take, because it is indistinguishable from
    a slow solve. Measured: ``fork`` never returns, ``spawn`` and ``forkserver``
    both do. This cannot be enforced here (a remote executor has no start
    method to inspect, and reaching into ``_mp_context`` is not a contract), so
    it is stated instead: pass the context, and give the entry point the
    ``__main__`` guard that ``spawn`` requires.

    .. code-block:: python

        ctx = multiprocessing.get_context('spawn')
        with ProcessPoolExecutor(4, mp_context=ctx) as pool:
            runs = lps.solve_over(model, sources, axis, keep=('p',), executor=pool)

    ``shared_fs`` says whether the executor's workers can read the caller's
    paths. It is the one fact that cannot be inferred from the Executor
    protocol, and it only affects sources that do *not* carry the slice key: a
    sliced source is read and filtered per slice either way.
    """
    if carry and executor is not None:
        raise LpspecError(
            'carry and executor are mutually exclusive: a carried value makes slice i+1 depend on '
            "slice i's answer, so the slices cannot run concurrently. Drop the executor, or drop the carry."
        )
    cuts = axis._slices(sources) if isinstance(axis, (EachCoordinate, EachWindow)) else list(axis)
    key_name = axis._key_name if isinstance(axis, (EachCoordinate, EachWindow)) else 'slice'
    if carry and isinstance(axis, (EachCoordinate, EachWindow)) and not axis._is_ordered():
        raise LpspecError(
            f'carry needs an ordered axis: {axis!r} has no defined "next" slice for a value to move into. '
            f'EachCoordinate(..., ordered=True) says the coordinates are a sequence.'
        )
    if not cuts:
        raise DataError('the axis produced no slices')

    keep = tuple(keep)
    # the caller's own coords are merged under the axis's, which owns the dim it
    # re-indexed; popped once rather than per slice, so the loop stays pure
    caller_coords = dict(build_kwargs.pop('coords', None) or {})
    options = dict(solver_options or {})
    rows: list[dict[str, Any]] = []
    primals: dict[str, list[pl.DataFrame]] = {name: [] for name in keep}

    def absorb(key: Any, meta: dict[str, Any], frames: dict[str, pl.DataFrame]) -> None:
        rows.append({key_name: key, **meta})
        for name, frame in frames.items():
            primals[name].append(frame.select(pl.lit(key).alias(key_name), pl.all()))

    if executor is None:
        state: dict[str, Any] = {}
        for key, sliced, coords in cuts:
            meta, frames = _run_slice(
                model,
                {**sliced, **state},
                {**caller_coords, **coords},
                solver_name,
                options,
                keep,
                False,
                **build_kwargs,
            )
            absorb(key, meta, frames)
            if carry:
                state = _carried(carry, frames, key)
    else:
        # A thread pool runs in this process, so encoding would be a parquet
        # round trip for a boundary that is not there — 31% of a thread-pool
        # sweep, measured. `ThreadPoolExecutor` is public stdlib, so this is a
        # documented class rather than a reach into an executor's internals;
        # every other executor is assumed to cross a boundary, because none of
        # them can be asked.
        crosses = not isinstance(executor, ThreadPoolExecutor)
        futures = [
            executor.submit(
                _run_slice,
                model,
                _encode(sliced, shared_fs=shared_fs) if crosses else dict(sliced),
                {**caller_coords, **coords},
                solver_name,
                options,
                keep,
                crosses,
                **build_kwargs,
            )
            for _key, sliced, coords in cuts
        ]
        # in slice order, never completion order — a sweep must not reorder itself
        for (key, _sliced, _coords), future in zip(cuts, futures, strict=True):
            meta, returned = future.result()
            absorb(
                key,
                meta,
                {n: pl.read_parquet(io.BytesIO(v)) for n, v in returned.items()} if crosses else returned,
            )

    return Runs(
        key_name=key_name,
        meta=pl.DataFrame(rows),
        _primals={name: pl.concat(frames) for name, frames in primals.items() if frames},
    )


def _run_slice(
    model: Any,
    encoded: dict[str, Any],
    coords: dict[str, Any],
    solver_name: str,
    solver_options: dict[str, Any],
    keep: tuple[str, ...],
    encode_out: bool,
    **build_kwargs: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One slice, start to finish, over plain data.

    Module-level and closure-free on purpose: a remote executor has to pickle
    what it is handed, and a bound method or a lambda over the axis object
    cannot cross. Everything in the signature is a path, a frame, a string or a
    number.
    """
    sources = _decode(encoded)
    with _solve(
        model,
        sources,
        solver_options=solver_options or None,
        solver_name=solver_name,
        **({'coords': coords} if coords else {}),
        **build_kwargs,
    ) as result:
        meta = {
            'status': result.status,
            'termination_condition': result.termination_condition,
            'objective': result.objective if result.has_primal else float('nan'),
        }
        frames = {name: result.primal(name) for name in keep} if result.has_primal else {}
        if not encode_out:
            return meta, frames
        out: dict[str, Any] = {}
        for name, frame in frames.items():
            buffer = io.BytesIO()
            frame.write_parquet(buffer, compression=_COMPRESSION)
            out[name] = buffer.getvalue()
        return meta, out


def _carried(
    carry: Mapping[str, tuple[str, int | None]],
    frames: Mapping[str, pl.DataFrame],
    key: Any,
) -> dict[str, Any]:
    """The next slice's carried parameters, read out of this slice's primals."""
    state: dict[str, Any] = {}
    for parameter, (variable, index) in carry.items():
        if variable not in frames:
            raise LpspecError(
                f'carry reads variable {variable!r}, which this run did not keep. '
                f'Add it to keep=(...) — the carry is read from the same frames.'
            )
        values = frames[variable]['value']
        if index is None:
            if values.len() != 1:
                raise LpspecError(
                    f'carry {parameter!r} <- {variable!r} used index=None, which means "the only row", '
                    f'but {variable!r} has {values.len()} rows in slice {key!r}. Name the index.'
                )
            picked = values[0]
        else:
            if not -values.len() <= index < values.len():
                raise LpspecError(
                    f'carry {parameter!r} <- ({variable!r}, {index}) is out of range: '
                    f'{variable!r} has {values.len()} rows in slice {key!r}'
                )
            picked = values[index]
        state[parameter] = pl.DataFrame({'value': [float(picked)]})
    return state


# ---------------------------------------------------------------------------
# the wire
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Blob:
    """Parquet bytes on their way to a worker."""

    data: bytes


def _encode(sources: Mapping[str, Any], *, shared_fs: bool) -> dict[str, Any]:
    """Sources in the shape a worker can be handed.

    A path the workers can reach stays a path and costs nothing. A path they
    cannot reach travels as **its own bytes, untouched** — decoding and
    re-encoding a parquet file produces byte-identical output for 79x the CPU,
    so the caller must never be pushed into doing it by hand. Anything held in
    memory is written to parquet, which beats pickling the frame on both size
    and time.

    Not called on the sequential path at all: nothing crosses a boundary there,
    so encoding would be a round trip paid for nothing.
    """
    out: dict[str, Any] = {}
    for name, obj in sources.items():
        if isinstance(obj, (str, Path)):
            out[name] = str(obj) if shared_fs else _Blob(Path(obj).read_bytes())
            continue
        frame = _lazy(obj).collect()
        buffer = io.BytesIO()
        frame.write_parquet(buffer, compression=_COMPRESSION)
        out[name] = _Blob(buffer.getvalue())
    return out


def _decode(encoded: Mapping[str, Any]) -> dict[str, Any]:
    return {name: pl.read_parquet(io.BytesIO(v.data)) if isinstance(v, _Blob) else v for name, v in encoded.items()}


# ---------------------------------------------------------------------------
# reading a source without binding it
# ---------------------------------------------------------------------------


def _lazy(obj: Any) -> pl.LazyFrame:
    """One source as a lazy frame — a scan for a path, so a filter pushes down."""
    if isinstance(obj, (str, Path)):
        return pl.scan_parquet(obj)
    frame = as_frame(obj)
    if frame is None:
        raise DataError(
            f'cannot slice a source of type {type(obj).__name__} — pass a parquet path or a table '
            f'polars can read (polars, pyarrow, pandas)'
        )
    return frame


def _columns(obj: Any) -> list[str]:
    return _lazy(obj).collect_schema().names()


def _carrying(sources: Mapping[str, Any], dim: str) -> list[str]:
    """Every source carrying *dim* — the ones a slice has to filter.

    Derived rather than declared: a source that carries the slice key and is
    *not* filtered produces a duplicate-coordinate error at bind time, so the
    derivation cannot silently miss one.
    """
    return [name for name, obj in sources.items() if dim in _columns(obj)]


def _distinct(obj: Any, dim: str) -> Iterator[Any]:
    yield from _lazy(obj).select(pl.col(dim).unique()).collect()[dim].to_list()
