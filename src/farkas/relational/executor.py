"""Polars executor: fill the model frames, then hand them to a sink.

The lane is described in ARCHITECTURE.md, "The relational lane".

This module owns the *data*. It does not own the query —
:mod:`farkas.relational.compiler` turns plan nodes into lazy frames and reads
nothing — and it does not own the way a model leaves:
:mod:`farkas.relational.sinks` drains the frames into LP text or into HiGHS,
one module per sink.

What is left here is what genuinely needs the data: binding sources, building
the dim frames, assigning labels, assembling ``cols``/``obj``/``rows``/``A``,
and joining solution values back to coordinates.

**Labels are the one place order is load-bearing.** ``var_label`` *is* the
solver column index and ``row`` *is* the solver row index, so both are assigned
by sorting the masked coordinate product on its dimensions' declared ordinals
and numbering the result. Variables and constraint rows are the same operation
over different frames (:meth:`PolarsExecutor._label_frame`); writing it twice is
how the two would come to disagree about which coordinate gets which index.

Operators are still classified by coordinate locality: pointwise (joins, masks,
group_sum) and bounded-halo (roll: t±k) compose freely; genuinely global
operators (running sums, normalisations) are rejected at lowering with a rewrite
hint, e.g. running sum → state-variable recurrence.

polars is the only table this module knows: sources arrive as
:class:`polars.LazyFrame` (or a parquet path polars scans itself) and results
leave as :class:`polars.DataFrame`, which is Arrow-backed and exports the
PyCapsule protocol — so a caller can hand it to pyarrow, pandas or duckdb
without this package depending on any of them. ``sources.tidy_sources`` is
where a caller's pandas/pyarrow/xarray object is turned into one, and
:meth:`Result.to_pandas` is the one exit that asks for pandas back.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from farkas.errors import DataError, LanguageError, LinopyYamlError, NoSolutionError
from farkas.relational import plan, sinks
from farkas.relational.compiler import PolarsCompiler, TermFragment, _ordinal
from farkas.relational.frames import as_frame

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import pandas as pd
    import polars as pl

    from farkas.relational.status import SolveStatus

#: The four frames a sink reads, as schemas. Stated here because the executor
#: is what fills them and an empty model still has to have them.
_COLS = ('col', 'lb', 'ub', 'vtype')
_OBJ = ('col', 'coeff')
_ROWS = ('row', 'sense', 'rhs')
_MATRIX = ('row', 'col', 'coeff')

#: Which of those columns is a label, a number, or a word — the whole dtype
#: vocabulary the four frames use between them.
_DTYPES = {
    'col': 'Int64', 'row': 'Int64',
    'lb': 'Float64', 'ub': 'Float64', 'rhs': 'Float64', 'coeff': 'Float64',
    'vtype': 'String', 'sense': 'String',
}  # fmt: skip

#: Scratch column carrying a source row's position while first-occurrence
#: order is computed. The spaces make it unrepresentable as a declared name, so
#: it cannot collide with a column the caller's index already has.
_ROW_POSITION = '__row position__'


#: Deprecated. The engine's failures are now split between
#: :class:`~farkas.errors.LanguageError` (the program says something the
#: engine cannot build) and :class:`~farkas.errors.DataError` (a source is
#: missing or the wrong shape). This alias is their common base, so an existing
#: ``except RelationalBuildError`` keeps catching everything it used to.
RelationalBuildError = LinopyYamlError


@dataclass
class Result:
    """What a solve returned — the outcome, and access to any values.

    Named for linopy's envelope rather than its ``Solution``, and for a
    reason local to this class: it is returned when the solve produced
    *nothing*, so "solution" would be a lie in exactly the case a caller most
    needs to notice. ``primal(name)`` joins labels back to coords.

    **No lifetime to manage.** The built model is frames owned by this
    process, so the readers stay valid for as long as the object does.
    :meth:`close` and the context-manager protocol exist because releasing a
    large model early is worth doing and ``with`` reads well, but nothing
    breaks if you skip them.
    """

    _status: SolveStatus
    _objective: float
    _executor: PolarsExecutor
    _has_duals: bool = False

    @property
    def status(self) -> str:
        """Coarse outcome: ``ok`` / ``warning`` / ``error`` / ``aborted`` / ``unknown``."""
        return self._status.status

    @property
    def termination_condition(self) -> str:
        """What the solver said — ``optimal``, ``infeasible``, ``time_limit``…"""
        return self._status.termination_condition

    @property
    def is_ok(self) -> bool:
        """linopy's rollup: not an error, an abort or a refusal."""
        return self._status.is_ok

    @property
    def has_primal(self) -> bool:
        """Whether there are values to read — what the accessors gate on.

        Narrower than :attr:`is_ok`: a run stopped at a time limit before
        finding any incumbent is ``ok`` and has nothing to read.
        """
        return self._status.is_readable

    @property
    def objective(self) -> float:
        """The objective value, or ``nan`` when there is no solution."""
        return self._objective

    def _require_solution(self, what: str) -> None:
        if self._status.is_readable:
            return
        raise NoSolutionError(
            f'cannot read {what}: the solve terminated {self.termination_condition!r} '
            f'({self._status.solver_wording}), so there are no values to read. Test '
            f'`has_primal` first. This raises rather than returning, because the solver '
            f'hands back a full-length vector of zeros either way and it is '
            f'indistinguishable from an answer.'
        )

    def primal(self, name: str) -> pl.DataFrame:
        """The tidy solution of *name* — ``(dims…, value)``.

        A :class:`polars.DataFrame`, which is the engine's own shape and also
        Arrow-backed: it exports the PyCapsule protocol, so pyarrow, pandas and
        duckdb all take it without a conversion this package has to depend on.
        :meth:`to_pandas`, :meth:`to_dataarray` and :meth:`to_parquet` are the
        named bridges out, each saying what it costs.
        """
        self._require_solution(f"the primal of '{name}'")
        return self._executor._primal(name)

    def dual(self, name: str) -> pl.DataFrame:
        """Shadow prices of constraint *name*: ``(dims…, value)``.

        The same label join :meth:`primal` does, against the constraint's row
        frame rather than a variable's column frame. A nodal balance's dual is
        the price at that node, so this is a headline output and not a
        diagnostic.

        There are two ways to have nothing to return, and they are different
        failures. No values at all is :class:`~farkas.errors.NoSolutionError`,
        the same gate :meth:`primal` passes through. A solve that *did* leave
        values but no duals — any integer or binary variable makes them
        undefined — is not that one: the primals are perfectly readable, and
        only this quantity is missing. Both raise rather than returning zeros.

        Duals exist only on this sink: a model written to LP and solved
        elsewhere never passes back through here.
        """
        self._require_solution(f"the dual of '{name}'")
        if not self._has_duals:
            raise LinopyYamlError(self._executor._no_duals_reason(self.termination_condition))
        return self._executor._dual(name)

    def to_pandas(self, name: str) -> pd.DataFrame:
        """:meth:`primal` as a tidy :class:`pandas.DataFrame`.

        Requires pandas, which is not a dependency of this package — it ships
        with the ``[linopy]`` extra, or install it directly.

        Built column by column rather than through polars' own ``to_pandas``,
        which reaches for pyarrow: a bridge that pulls in a third library to
        cross one boundary is not a bridge anyone wants. The cost is a copy of
        a table that is already tidy and already the size of the answer.
        """
        import pandas as pd

        self._require_solution(f"the primal of '{name}'")
        frame = self._executor._primal(name)
        return pd.DataFrame({column: frame[column].to_numpy() for column in frame.columns})

    def to_dataarray(self, name: str) -> Any:
        """``primal(name)`` as a labelled :class:`xarray.DataArray`.

        Named for what it returns, pairing with :meth:`to_dataset` — pandas'
        ``to_xarray`` returns either type depending on the receiver, and that
        ambiguity is not worth inheriting when both forms exist here.

        Long tables are the right shape for slicing and joining, and the wrong
        one for the array math post-processing is mostly made of —
        ``.sel(generator='wind')``, resampling, duration curves. This is the
        bridge, and it is one line so that its absence never reads as "results
        are hard to use".

        Goes through ``pandas.DataFrame.to_xarray()`` rather than importing
        xarray here: the streaming lane is xarray-free (hard rule 2), and
        pandas does the optional import for us. Requires xarray to be
        installed (it ships with the ``[linopy]`` extra, and brings pandas with
        it); missing coordinate combinations come back as NaN, since a masked
        variable has no row.
        """
        frame = self.to_pandas(name)
        dims = [c for c in frame.columns if c != 'value']
        if not dims:
            return frame['value'].to_xarray().rename(name)
        # the tidy column is 'value'; the array should carry the variable's name
        return frame.set_index(dims).to_xarray()['value'].rename(name)

    def to_dataset(self, *names: str) -> Any:
        """Variables as one :class:`xarray.Dataset`; all of them by default.

        A small model wants every variable at once — that is what linopy's
        ``model.solution`` gives you, and naming them would be busywork.

        Costs what it says: each variable arrives *dense* over its own dims, so
        the Dataset holds the full coordinate product regardless of what the
        mask removed, and all of them at once. On a large model, name the few
        you need or use :meth:`to_parquet`, which streams.
        """
        assert self._executor._program is not None
        wanted = names or tuple(v.name for v in self._executor._program.variables)
        first, *rest = wanted
        dataset = self.to_dataarray(first).to_dataset(name=first)
        for name in rest:
            dataset[name] = self.to_dataarray(name)
        return dataset

    def to_parquet(self, directory: str | Path) -> dict[str, Path]:
        """Stream per-variable solution tables to ``directory`` (one parquet
        file per variable, columns ``(dims..., value)``). Returns name → path.

        polars sinks each frame straight to disk, so the solution is never
        copied into a second in-process representation on the way out. Other
        formats may follow the same shape (``to_csv``, ...) — parquet is the
        canonical one.
        """
        self._require_solution('the solution')
        return self._executor._solution_to_parquet(Path(directory))

    def close(self) -> None:
        """Release the built model early. Optional — see the class docstring."""
        self._executor.close()

    def __enter__(self) -> Result:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        self.close()
        return False


class PolarsExecutor:
    """Build a :class:`Program` into polars frames, then sink it."""

    def __init__(self) -> None:
        self._program: plan.Program | None = None
        self._compiler: PolarsCompiler | None = None
        #: Read by the compiler at compile time, not at construction — see
        #: :class:`~farkas.relational.compiler.PolarsCompiler`.
        self._parameters: dict[str, pl.LazyFrame] = {}
        self._dimensions: dict[str, pl.LazyFrame] = {}
        self._variables: dict[str, pl.LazyFrame] = {}
        self._constraints: dict[str, pl.LazyFrame] = {}
        self._bool_params: set[str] = set()
        self._dim_card: dict[str, int] = {}
        self._cols: pl.DataFrame | None = None
        self._obj: pl.DataFrame | None = None
        self._rows: pl.DataFrame | None = None
        self._matrix: pl.DataFrame | None = None
        self._sol: pl.DataFrame | None = None
        self._duals: pl.DataFrame | None = None
        self._n_cols = 0
        self._n_rows = 0
        self._obj_const = 0.0
        self._obj_sense: str = 'min'

    # ------------------------------------------------------------------
    # build
    # ------------------------------------------------------------------

    def build(self, program: plan.Program, sources: Mapping[str, Any]) -> None:
        """Load sources, create dim/parameter frames, variables, constraints."""

        self._program = program

        for p in program.parameters:
            self._create_param_frame(p, sources)
        self._create_dim_frames(program, sources)

        # the compiler is built after the data, because two of its answers
        # depend on it: sum over an absent dim scales by that dim's size, and
        # `defined` on a boolean parameter tests the value, not its finiteness
        self._compiler = PolarsCompiler(
            program,
            dict(self._dim_card),
            frozenset(self._bool_params),
            self._parameters,
            self._dimensions,
            self._variables,
        )

        # one declaration at a time, concatenated at the end: a declaration's
        # rows are independent of every other's, which is what lets the whole
        # model be a handful of frames rather than a graph
        cols = [self._build_variable(v) for v in program.variables]
        built = [self._build_constraint(c) for c in program.constraints]
        objective = self._build_objective(program.objective)

        self._cols = _stack(cols, _COLS)
        self._rows = _stack([r for r, _ in built], _ROWS)
        self._matrix = _stack([m for _, m in built if m is not None], _MATRIX)
        self._obj = _stack([objective] if objective is not None else [], _OBJ)

    @property
    def _q(self) -> PolarsCompiler:
        assert self._compiler is not None, 'build() has not run'
        return self._compiler

    # ------------------------------------------------------------------
    # sources
    # ------------------------------------------------------------------

    def _create_param_frame(self, p: plan.ParameterDeclaration, sources: Mapping[str, Any]) -> None:
        import polars as pl

        if p.name not in sources:
            raise DataError(f"no source bound for parameter '{p.name}'")
        frame = self._source_frame(p.name, sources[p.name])
        wanted = [*p.dims, 'value']
        missing = set(wanted) - set(frame.collect_schema().names())
        if missing:
            raise DataError(
                f"source for parameter '{p.name}' is missing columns {sorted(missing)} "
                f"(need dims {list(p.dims)} plus 'value')"
            )
        frame = frame.select(wanted).collect().lazy()
        self._check_one_row_per_coordinate(p, frame)
        if frame.collect_schema()['value'] == pl.Boolean:
            self._bool_params.add(p.name)
        self._parameters[p.name] = frame

    def _check_one_row_per_coordinate(self, p: plan.ParameterDeclaration, frame: pl.LazyFrame) -> None:
        """A parameter is a function of its dims: one row per coordinate.

        Two rows for one coordinate has no defined meaning — the eager lane
        refuses to lay such a source out at all — so it is a data bug worth
        naming rather than silently resolving into a sum.

        Checking it also *earns* something: knowing every parameter is keyed is
        what lets the assembly skip an aggregate over every nonzero in the
        model (see ``TermFragment.keyed``). The check costs one pass over the
        source, which is orders of magnitude smaller than the matrix it saves
        aggregating.
        """
        import polars as pl

        if not p.dims:
            return
        duplicated = frame.group_by(p.dims).agg(pl.len().alias('n')).filter(pl.col('n') > 1).head(3).collect()
        if duplicated.height == 0:
            return
        shown = '; '.join(
            ', '.join(f'{d}={row[d]!r}' for d in p.dims) + f' ({row["n"]} rows)'
            for row in duplicated.iter_rows(named=True)
        )
        raise DataError(
            f"parameter '{p.name}' has more than one row for a coordinate: {shown}. "
            f'A parameter is a function of its dims, so which value applies is undefined — '
            f'aggregate the source to one row per {list(p.dims)} before binding it.'
        )

    def _source_frame(self, name: str, source: Any) -> pl.LazyFrame:
        import polars as pl

        if isinstance(source, (str, Path)):
            return pl.scan_parquet(source)
        frame = as_frame(source)
        if frame is not None:
            return frame
        raise DataError(
            f"source for '{name}' must be a parquet path or a table polars can "
            f'read — polars, pyarrow, pandas (got {type(source).__name__})'
        )

    def _create_dim_frames(self, program: plan.Program, sources: Mapping[str, Any]) -> None:
        import polars as pl

        dims: set[str] = set()
        for v in program.variables:
            dims.update(v.dims)
        for c in program.constraints:
            dims.update(c.dims)
        for p in program.parameters:
            dims.update(p.dims)

        for d in sorted(dims):
            coordinates = dict(program.dimension(d).coordinates)
            if d in sources:
                # explicit index: ordinals follow declared/coords order, so
                # Translate's positional semantics match xarray/linopy exactly
                # even for non-monotonic or string coordinates
                table = self._explicit_dim_frame(d, sources[d], sorted(coordinates))
            else:
                if coordinates:
                    raise DataError(
                        f"dimension '{d}' declares coordinates {sorted(coordinates)} but has "
                        f"no index source. Pass one under key '{d}' (a parquet path or frame "
                        f'carrying columns {[d, *sorted(coordinates)]}) — a coordinate cannot '
                        f'be inferred from the parameters that happen to use the dimension.'
                    )
                params = [p for p in program.parameters if d in p.dims]
                if not params:
                    raise DataError(
                        f"dimension '{d}' has no source: no parameter carries it and "
                        f"no explicit index was provided under key '{d}'"
                    )
                # derived dims carry no declared order — sorted values are the
                # deterministic fallback (pass an explicit index to control it)
                stacked = pl.concat([self._parameters[p.name].select(pl.col(d).alias('val')) for p in params])
                table = stacked.unique().sort('val').with_row_index('ord').with_columns(pl.col('ord').cast(pl.Int64))
            materialised = table.collect()
            self._dimensions[d] = materialised.lazy()
            self._dim_card[d] = materialised.height

        # Coordinate containment, once every dim frame exists: a coordinate's
        # values must be labels of the dimension it targets. This is the check
        # that stops a mistyped label from vanishing in the inner join that
        # places its terms — it built and solved, with the term silently gone.
        for d in sorted(dims):
            for cname, target in sorted(program.dimension(d).coordinates):
                if target not in self._dim_card:
                    raise DataError(
                        f"dimension '{d}' coordinate '{cname}' targets '{target}', which "
                        f'no declaration in this model uses, so it has no coordinate set '
                        f'to check against'
                    )
                self._check_coordinate_containment(d, cname, target)

    def _explicit_dim_frame(self, d: str, source: Any, names: list[str]) -> pl.LazyFrame:
        import polars as pl

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
        self._check_coordinates_single_valued(d, names, frame)
        # first occurrence of a label is its position, and polars preserves
        # row order, so the position is simply the row index
        return (
            frame.select(d, *names)
            .with_row_index(_ROW_POSITION)
            .group_by(d)
            .agg(pl.col(_ROW_POSITION).min(), *(pl.col(c).first() for c in names))
            .sort(_ROW_POSITION)
            .with_row_index('ord')
            .select(pl.col(d).alias('val'), pl.col('ord').cast(pl.Int64), *names)
        )

    def _check_coordinates_single_valued(self, d: str, names: list[str], frame: pl.LazyFrame) -> None:
        """One label, one coordinate value — two rows disagreeing is a data bug."""
        import polars as pl

        for c in names:
            bad = frame.group_by(d).agg(pl.col(c).n_unique().alias('n')).filter(pl.col('n') > 1).collect().height
            if bad:
                raise DataError(
                    f"dimension '{d}': {bad} label(s) carry more than one value for "
                    f"coordinate '{c}'. A coordinate is single-valued per label — "
                    f'reduce the source to one row per {d}, or model the relation as a '
                    f'parameter instead.'
                )

    def _check_coordinate_containment(self, d: str, cname: str, target: str) -> None:
        """Every coordinate value must be a label of the dimension it targets.

        A *null* value is not a violation: it says the label belongs to no
        group, which is the same row-absence idiom the rest of the engine uses
        for "not present". Only a value that is present and unknown is a typo,
        and that is the case worth stopping — it would drop terms silently.
        """
        import polars as pl

        known = self._dimensions[target].select(pl.col('val').alias(cname))
        bad = (
            self._dimensions[d]
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

    # ------------------------------------------------------------------
    # labels — the solver's own indices
    # ------------------------------------------------------------------

    def _label_frame(
        self,
        dims: tuple[str, ...],
        where: plan.Predicate | None,
        label: str,
        start: int,
    ) -> tuple[pl.DataFrame, int]:
        """The masked coord product of *dims* with a dense *label* from *start*.

        Variables (``var_label``) and constraint rows (``row``) are the same
        operation over different frames, which is why it is written once. The
        sort is on the dimensions' declared ordinals, so a label follows
        declaration order and ``var_label`` can *be* the solver column index
        with no remapping.
        """
        import polars as pl

        frame = self._q.frame(dims, where)
        materialised = (
            frame.sort([_ordinal(d) for d in dims])
            .select(*dims)
            .with_row_index(label, offset=start)
            .with_columns(pl.col(label).cast(pl.Int64))
            .collect(engine='streaming')
        )
        return materialised, start + materialised.height

    # ------------------------------------------------------------------
    # declarations
    # ------------------------------------------------------------------

    def _build_variable(self, v: plan.VariableDeclaration) -> pl.DataFrame:
        import polars as pl

        if not v.dims:
            raise LanguageError(f"variable '{v.name}' has no dims (scalars: use dims of size 1)")
        labelled, self._n_cols = self._label_frame(v.dims, v.where, 'var_label', self._n_cols)
        self._variables[v.name] = labelled.lazy()

        bounded = self._q.bounds(labelled.lazy(), v)
        cols = bounded.select(
            pl.col('var_label').alias('col'),
            pl.col('lb').cast(pl.Float64),
            pl.col('ub').cast(pl.Float64),
            pl.lit(v.variable_type, dtype=pl.String).alias('vtype'),
        ).collect(engine='streaming')

        bad = cols.filter(pl.col('lb').is_null() | pl.col('ub').is_null()).height
        if bad:
            raise DataError(
                f"variable '{v.name}': {bad} rows have NULL bounds — a bound parameter "
                f'is missing values for some coordinates'
            )
        return cols

    def _build_constraint(self, c: plan.ConstraintDeclaration) -> tuple[pl.DataFrame, pl.DataFrame | None]:
        import polars as pl

        if not c.dims:
            raise LanguageError(f"constraint '{c.name}' has no dims")
        lhs = self._q.expression(c.lhs, f"constraint '{c.name}' lhs")
        rhs = self._q.expression(c.rhs, f"constraint '{c.name}' rhs")
        # normalise: terms on the left (rhs terms negated), consts on the right
        terms = [(p, 1.0) for p in lhs.terms] + [(p, -1.0) for p in rhs.terms]
        consts = [(p, 1.0) for p in rhs.consts] + [(p, -1.0) for p in lhs.consts]
        for p, _ in [*terms, *consts]:
            extra = set(p.dims) - set(c.dims)
            if extra:
                raise LanguageError(
                    f"constraint '{c.name}': expression has dims {sorted(extra)} outside "
                    f'foreach {list(c.dims)} — missing a Sum/GroupSum?'
                )

        labelled, self._n_rows = self._label_frame(c.dims, c.where, 'row', self._n_rows)
        frame = labelled.lazy()
        self._constraints[c.name] = frame  # kept for the dual read-back

        # right-hand side: each const fragment aggregated to its coordinates,
        # then left-joined. A coordinate the fragment has no row for
        # contributes zero — the null-is-absent idiom, not a missing value.
        accumulated = pl.lit(0.0, dtype=pl.Float64)
        carrier = frame
        for i, (p, sign) in enumerate(consts):
            column = f'__const {i}__'
            aggregated = self._q.constant_scalar(p).rename({'cval': column})
            carrier = (
                carrier.join(aggregated, on=list(p.dims), how='left')
                if p.dims
                else carrier.join(aggregated, how='cross')
            )
            accumulated = accumulated + sign * pl.col(column).fill_null(0.0)

        rows = carrier.select(
            'row',
            pl.lit(c.sense, dtype=pl.String).alias('sense'),
            accumulated.cast(pl.Float64).alias('rhs'),
        ).collect(engine='streaming')

        if not terms:
            return rows, None

        pieces = []
        for p, sign in terms:
            placed = frame.join(p.frame, on=list(p.dims), how='inner') if p.dims else frame.join(p.frame, how='cross')
            pieces.append(
                placed.select(
                    'row',
                    pl.col('var_label').alias('col'),
                    (sign * pl.col('coeff')).cast(pl.Float64).alias('coeff'),
                )
            )
        # the terminal aggregate: where duplicates from Sum and GroupSum —
        # which project rather than aggregate — finally collapse. Skipped when
        # no two rows can share a (row, col): one keyed fragment placed against
        # distinct constraint rows cannot produce a column twice, and this is
        # an aggregate over every nonzero in the model.
        stacked = pl.concat(pieces)
        if _needs_aggregate([fragment for fragment, _ in terms]):
            stacked = stacked.group_by('row', 'col').agg(pl.col('coeff').sum())
        return rows, stacked.collect(engine='streaming')

    def _build_objective(self, o: plan.ObjectiveDeclaration) -> pl.DataFrame | None:
        import polars as pl

        comp = self._q.expression(o.expression, 'objective')
        for p in comp.consts:
            if p.dims:
                raise LanguageError(
                    'objective constant part has dims — wrap parameter terms in '
                    'Mul with a Var, or pre-aggregate to a scalar'
                )
            self._obj_const += p.frame.select(pl.col('cval').sum()).collect().item() or 0.0
        self._obj_sense = o.sense
        if not comp.terms:
            return None
        pieces = [p.frame.select(pl.col('var_label').alias('col'), pl.col('coeff')) for p in comp.terms]
        stacked = pl.concat(pieces)
        if _needs_aggregate(comp.terms):
            stacked = stacked.group_by('col').agg(pl.col('coeff').sum())
        return stacked.collect(engine='streaming')

    # ------------------------------------------------------------------
    # sinks — see relational/sinks/; the executor only supplies the frames
    # ------------------------------------------------------------------

    def _tables(self) -> sinks.ModelTables:
        assert self._cols is not None and self._obj is not None
        assert self._rows is not None and self._matrix is not None
        return sinks.ModelTables(
            cols=self._cols,
            obj=self._obj,
            rows=self._rows,
            matrix=self._matrix,
            column_count=self._n_cols,
            row_count=self._n_rows,
            objective_sense=self._obj_sense,
            objective_constant=self._obj_const,
        )

    def write_lp(self, path: str | Path) -> None:
        """Sink the built model to an LP file."""
        sinks.write_lp_file(self._tables(), path)

    def solve(
        self,
        batch_rows: int = 100_000,
        solver_options: Mapping[str, Any] | None = None,
    ) -> Result:
        """Sink the built model straight into HiGHS and solve it.

        ``solver_options`` is forwarded verbatim to the solver, the way
        linopy's is — ``{'time_limit': 60, 'mip_rel_gap': 0.01}``.
        """
        status, objective, primal, dual = sinks.solve_direct(self._tables(), batch_rows, solver_options)
        self._sol, self._duals = primal, dual
        return Result(_status=status, _objective=objective, _executor=self, _has_duals=dual is not None)

    def _solution_frame(self, name: str) -> pl.LazyFrame:
        """The tidy solution of variable *name*: ``(dims…, value)``.

        A label join, never a dense array — which is what makes reading results
        back cost the same whichever sink asked for them.
        """
        assert self._program is not None
        assert self._sol is not None, 'no solve has stored a primal'
        dims = self._program.variable(name).dims
        return (
            self._variables[name]
            .join(self._sol.lazy(), left_on='var_label', right_on='col', how='inner')
            .select(*dims, 'value')
        )

    def _primal(self, name: str) -> pl.DataFrame:
        return self._solution_frame(name).collect(engine='streaming')

    def _dual(self, name: str) -> pl.DataFrame:
        """The adjoint of :meth:`_solution_frame` — the same join, against the
        row labels instead of the column ones."""
        assert self._program is not None
        assert self._duals is not None, 'no solve has stored duals'
        dims = self._program.constraint(name).dims
        return (
            self._constraints[name]
            .join(self._duals.lazy(), on='row', how='inner')
            .select(*dims, 'value')
            .collect(engine='streaming')
        )

    def _no_duals_reason(self, termination_condition: str) -> str:
        """Why a solve that *did* leave values still has no duals.

        Integrality is decidable from the program, so the model's own reason is
        preferred over the solver's: naming the variable is actionable in a way
        that "HiGHS reported no dual solution" is not.
        """
        assert self._program is not None
        discrete = sorted(v.name for v in self._program.variables if v.variable_type != 'continuous')
        if discrete:
            names = ', '.join(f"'{n}'" for n in discrete)
            return (
                f'duals are undefined for a mixed-integer model: {names} '
                f'{"is" if len(discrete) == 1 else "are"} not continuous. '
                f'Drop the integrality to price the LP relaxation instead.'
            )
        return (
            f'the solver returned no dual solution, though the solve terminated '
            f'{termination_condition!r}. Duals come from a simplex basis, which a '
            f'run stopped short of one does not have.'
        )

    def _solution_to_parquet(self, directory: Path) -> dict[str, Path]:
        assert self._program is not None
        directory.mkdir(parents=True, exist_ok=True)
        written: dict[str, Path] = {}
        for v in self._program.variables:
            out = directory / f'{v.name}.parquet'
            self._solution_frame(v.name).sink_parquet(out)
            written[v.name] = out
        return written

    # ------------------------------------------------------------------

    def close(self) -> None:
        """Drop the built model. Optional — see :class:`Result`."""
        self._cols = self._obj = self._rows = self._matrix = None
        self._sol = self._duals = None
        self._variables.clear()
        self._constraints.clear()
        self._parameters.clear()
        self._dimensions.clear()

    def __enter__(self) -> PolarsExecutor:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        self.close()
        return False


def _needs_aggregate(terms: Sequence[TermFragment]) -> bool:
    """Whether stacking *terms* can put two rows on one solver column.

    Named for the answer rather than the condition: this is read as "sum the
    stack or not", and an inverted test here is a wrong model rather than a
    slow one.

    Two ways it can. **Two fragments** may both carry the same variable —
    ``x + 2 * x`` is one row each and one column — so anything but a single
    fragment has to be summed. And a single fragment that is not
    :attr:`~farkas.relational.compiler.TermFragment.keyed` already holds the
    same ``var_label`` twice on its own.

    Everything else — including the placement join against the constraint
    frame, whose rows are distinct by construction — preserves distinctness,
    so the aggregate has nothing to do.
    """
    return len(terms) != 1 or not terms[0].keyed


def _stack(frames: list[pl.DataFrame], columns: tuple[str, ...]) -> pl.DataFrame:
    """Concatenate *frames*, or an empty frame of *columns* when there are none.

    The columns are named rather than inferred because a model may legitimately
    have nothing to stack — an objective with no variable terms, a constraint
    with no coefficients — and a sink still has to find what it reads.
    """
    import polars as pl

    if frames:
        return pl.concat(frames)
    return pl.DataFrame(schema={name: getattr(pl, _DTYPES[name]) for name in columns})
