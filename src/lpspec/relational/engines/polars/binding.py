"""What a caller's ``sources`` become: the frames the engine reads by name.

The boundary between *what was passed in* — parquet paths, any table exposing
the Arrow PyCapsule protocol — and *what the query is written against*. Binding
is the only phase that touches a caller's data; everything downstream reads
:class:`BoundSources` and nothing else.

**It is frozen, and that is the point.** The four things here are written once,
during the three passes below, and are then read exactly twice: to construct
the compiler and the labeller. Holding them as a value rather than as four
dicts on the executor is what says so — and it separates them from the one
registry that is deliberately *live*, the variable frames, which appear as
declarations are built and which a constraint compiled afterwards has to see.
Three adjacent ``dict[str, pl.LazyFrame]`` attributes could not say which of
them was which.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import polars as pl

from lpspec.errors import DataError
from lpspec.relational.engines.polars import data_validation
from lpspec.relational.frames import as_frame

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lpspec.relational import plan

#: Scratch column carrying a source row's position while first-occurrence
#: order is computed. The spaces make it unrepresentable as a declared name, so
#: it cannot collide with a column the caller's index already has.
_ROW_POSITION = '__row position__'


@dataclass(frozen=True)
class BoundSources:
    """The data a program is built against, after binding.

    ``parameters`` are tidy ``(dims…, value)``; ``dimensions`` are
    ``(val, ord, coordinates…)``. The other two are answers *read off* the data
    that the query needs and cannot re-derive cheaply: ``sum`` over an absent
    dim scales by that dim's size, and ``defined`` on a boolean parameter tests
    the value rather than its finiteness.

    ``cardinality`` is a dimension frame's height, cached here because deriving
    it later means collecting the frame again.
    """

    parameters: Mapping[str, pl.LazyFrame]
    dimensions: Mapping[str, pl.LazyFrame]
    cardinality: Mapping[str, int]
    boolean_parameters: frozenset[str]


def bind(program: plan.Program, sources: Mapping[str, Any]) -> BoundSources:
    """Adapt *sources* to the frames *program* is written against.

    Three passes, and the order is load-bearing. Dimensions with an index of
    their own come first, so a parameter's labels can be checked against them
    in the pass that binds it rather than in a second one over the same rows.
    The parameters follow. The remaining dimensions are *derived* from those
    parameters, so they cannot be built until they exist — and a derived
    dimension has no strangers to find, its labels being the union of what
    arrived.
    """
    binder = _Binder(program, sources)
    binder.sourced_dimensions()
    for p in program.parameters:
        binder.parameter(p)
    binder.remaining_dimensions()
    return BoundSources(
        parameters=binder.parameters,
        dimensions=binder.dimensions,
        cardinality=binder.cardinality,
        boolean_parameters=frozenset(binder.boolean),
    )


class _Binder:
    """The three passes' shared accumulator; discarded once :func:`bind` returns."""

    def __init__(self, program: plan.Program, sources: Mapping[str, Any]) -> None:
        self.program = program
        self.sources = sources
        self.parameters: dict[str, pl.LazyFrame] = {}
        self.dimensions: dict[str, pl.LazyFrame] = {}
        self.cardinality: dict[str, int] = {}
        self.boolean: set[str] = set()

    # -- parameters --------------------------------------------------------

    def parameter(self, p: plan.ParameterDeclaration) -> None:
        """Bind one parameter's source and register it as a tidy frame."""

        if p.name not in self.sources:
            raise DataError(f"no source bound for parameter '{p.name}'")
        frame = self._source_frame(p.name, self.sources[p.name])
        wanted = [*p.dims, 'value']
        missing = set(wanted) - set(frame.collect_schema().names())
        if missing:
            raise DataError(
                f"source for parameter '{p.name}' is missing columns {sorted(missing)} "
                f"(need dims {list(p.dims)} plus 'value')"
            )
        # streaming here and nowhere else in this file: this is the one
        # collect whose result is model-sized, so it is the one the engine
        # choice moves. `collect()` defaults to the in-memory engine — unlike
        # `sink_csv`, whose default resolves to streaming — and switching every
        # collect costs 29% on a small join-heavy model to save the same 0.15 GB
        # this one saves alone.
        frame = frame.select(wanted).collect(engine='streaming').lazy()
        # before the cast, not after: a dictionary-encoded column compares on
        # its codes, and widening it to strings first doubles the check
        data_validation.check_one_row_per_coordinate(p, frame, self.dimensions)
        frame = _plain_strings(frame, p.dims)
        if frame.collect_schema()['value'] == pl.Boolean:
            self.boolean.add(p.name)
        self.parameters[p.name] = frame

    def _source_frame(self, name: str, source: Any) -> pl.LazyFrame:
        if isinstance(source, (str, Path)):
            return pl.scan_parquet(source)
        frame = as_frame(source)
        if frame is not None:
            return frame
        raise DataError(
            f"source for '{name}' must be a parquet path or a table polars can "
            f'read — polars, pyarrow, pandas (got {type(source).__name__})'
        )

    # -- dimensions --------------------------------------------------------

    def sourced_dimensions(self) -> None:
        """Every dimension carrying its own index, before any parameter binds."""
        for d in sorted(self._declared_dims()):
            if d in self.sources:
                coordinates = sorted(dict(self.program.dimension(d).coordinates))
                self._register(d, self._explicit_frame(d, self.sources[d], coordinates))

    def remaining_dimensions(self) -> None:
        """Build every dimension's frame, then check its coordinates.

        A dimension with no explicit index has no declared order, so its labels
        are sorted. Containment runs once every frame exists: it stops a
        mistyped coordinate from vanishing in the join that places its terms,
        leaving a model that builds and solves without them.
        """

        dims = self._declared_dims()
        for d in sorted(dims):
            if d in self.dimensions:  # built by `sourced_dimensions`
                continue
            coordinates = dict(self.program.dimension(d).coordinates)
            if d in self.sources:
                table = self._explicit_frame(d, self.sources[d], sorted(coordinates))
            else:
                if coordinates:
                    raise DataError(
                        f"dimension '{d}' declares coordinates {sorted(coordinates)} but has "
                        f"no index source. Pass one under key '{d}' (a parquet path or frame "
                        f'carrying columns {[d, *sorted(coordinates)]}) — a coordinate cannot '
                        f'be inferred from the parameters that happen to use the dimension.'
                    )
                params = [p for p in self.program.parameters if d in p.dims]
                if not params:
                    raise DataError(
                        f"dimension '{d}' has no source: no parameter carries it and "
                        f"no explicit index was provided under key '{d}'"
                    )
                stacked = pl.concat([self.parameters[p.name].select(pl.col(d).alias('val')) for p in params])
                table = stacked.unique().sort('val').with_row_index('ord').with_columns(pl.col('ord').cast(pl.Int64))
            self._register(d, table)

        for d in sorted(dims):
            for cname, target in sorted(self.program.dimension(d).coordinates):
                if target not in self.cardinality:
                    raise DataError(
                        f"dimension '{d}' coordinate '{cname}' targets '{target}', which "
                        f'no declaration in this model uses, so it has no coordinate set '
                        f'to check against'
                    )
                data_validation.check_coordinate_containment(d, cname, target, self.dimensions)

    def _explicit_frame(self, d: str, source: Any, names: list[str]) -> pl.LazyFrame:
        """A dimension's ``(val, ord, coordinates…)`` from a caller's index.

        Ordinals follow the source's own order, so a translation moves by
        position exactly as the eager lane does even for string labels. A
        label's position is the row it first appears at.
        """

        if isinstance(source, (str, Path)):
            frame = pl.scan_parquet(source)
        else:
            frame = as_frame(source)
            if frame is None:
                raise DataError(
                    f"explicit index for dimension '{d}' must be a table polars can read "
                    f"with a '{d}' column, or a parquet path"
                )
        available = frame.collect_schema().names()
        if d not in available:
            raise DataError(
                f"explicit index for dimension '{d}' must be a table polars can read "
                f"with a '{d}' column, or a parquet path (has {available})"
            )
        missing = [c for c in names if c not in available]
        if missing:
            raise DataError(
                f"index for dimension '{d}' is missing declared coordinate column(s) {missing} (has {available})"
            )
        # One pass, and one collect. The single-valued check needs an
        # `n_unique` per coordinate grouped by `d`, which is the aggregate this
        # is already running — so the counts ride in it rather than costing a
        # `group_by` each, over a frame that is a scan and would be re-read
        # every time (#273).
        grouped = (
            frame.select(d, *names)
            .with_row_index(_ROW_POSITION)
            .group_by(d)
            .agg(
                pl.col(_ROW_POSITION).min(),
                *(pl.col(c).first() for c in names),
                *data_validation.nunique_exprs(names),
            )
            .sort(_ROW_POSITION)
            .with_row_index('ord')
            .collect()
        )
        data_validation.check_coordinates_single_valued(d, names, grouped)
        return grouped.lazy().select(pl.col(d).alias('val'), pl.col('ord').cast(pl.Int64), *names)

    def _register(self, d: str, table: pl.LazyFrame) -> None:
        materialised = table.collect()
        self.dimensions[d] = materialised.lazy()
        self.cardinality[d] = materialised.height

    def _declared_dims(self) -> set[str]:
        dims: set[str] = set()
        for v in self.program.variables:
            dims.update(v.dims)
        for c in self.program.constraints:
            dims.update(c.dims)
        for p in self.program.parameters:
            dims.update(p.dims)
        return dims


def _plain_strings(frame: pl.LazyFrame, dims: tuple[str, ...]) -> pl.LazyFrame:
    """Dim columns as plain strings, whatever encoding the source used.

    A dictionary-encoded parquet column reads back as ``Categorical``, which is
    what pandas writes for any repeated label and what any sane writer produces
    for a 12M-row table of node names. polars will not join ``Categorical``
    against ``String`` — the dim frames are built from the declared coordinate
    values and are plain — so the two would have to agree by luck.

    Casting the *source* side rather than the dim side is deliberate: the dim
    frame is the authority on what a coordinate is, and a source is whatever a
    caller happened to hand over.
    """
    categorical = [d for d, dtype in frame.collect_schema().items() if d in dims and dtype in (pl.Categorical, pl.Enum)]
    if not categorical:
        return frame
    return frame.with_columns(pl.col(d).cast(pl.String) for d in categorical)
