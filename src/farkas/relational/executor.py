"""Polars executor: fill the model frames, then hand them to a sink.

Owns the *data* — sources, dim frames, labels, the four model frames, and the
join that reads solution values back. Owns neither the query
(:mod:`farkas.relational.compiler`) nor the exit (:mod:`.sinks`). The lane is
described in ARCHITECTURE.md.

**Labels are the one place order is load-bearing.** ``var_label`` *is* the
solver column index and ``row`` its row index, so both come from
:meth:`PolarsExecutor._label_frame` — written once, because two copies would
disagree about which coordinate gets which index.
"""

from __future__ import annotations

import math
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

    Named for linopy's envelope rather than its ``Solution``: it is returned
    when the solve produced *nothing*, so "solution" would be a lie in exactly
    the case a caller most needs to notice.

    **No lifetime to manage.** The model is frames this process owns, so the
    readers stay valid as long as the object does; :meth:`close` releases a
    large one early but nothing breaks without it.
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

        Narrower than :attr:`is_ok`: a run stopped at a time limit before any
        incumbent is ``ok`` with nothing to read.
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
        """The tidy solution of *name* — ``(dims…, value)``."""
        self._require_solution(f"the primal of '{name}'")
        return self._executor._primal(name)

    def dual(self, name: str) -> pl.DataFrame:
        """Shadow prices of constraint *name*: ``(dims…, value)``.

        :meth:`primal`'s join against the row frame rather than a column one.

        The two empty cases are different failures and both raise rather than
        return zeros: no values at all is
        :class:`~farkas.errors.NoSolutionError`, while primals without duals —
        any integer variable makes them undefined — raises
        :class:`~farkas.errors.LinopyYamlError`. Duals exist only on this sink.
        """
        self._require_solution(f"the dual of '{name}'")
        if not self._has_duals:
            raise LinopyYamlError(self._executor._no_duals_reason(self.termination_condition))
        return self._executor._dual(name)

    def to_pandas(self, name: str) -> pd.DataFrame:
        """:meth:`primal` as a tidy :class:`pandas.DataFrame`.

        Needs pandas, which ships with the ``[linopy]`` extra. Built column by
        column, since polars' own ``to_pandas`` reaches for pyarrow.
        """
        import pandas as pd

        self._require_solution(f"the primal of '{name}'")
        frame = self._executor._primal(name)
        return pd.DataFrame({column: frame[column].to_numpy() for column in frame.columns})

    def to_dataarray(self, name: str) -> Any:
        """``primal(name)`` as a labelled :class:`xarray.DataArray`.

        The bridge to array post-processing — ``.sel``, resampling, duration
        curves. Needs the ``[linopy]`` extra; a masked coordinate has no row
        and comes back NaN.
        """
        frame = self.to_pandas(name)
        dims = [c for c in frame.columns if c != 'value']
        if not dims:
            return frame['value'].to_xarray().rename(name)
        return frame.set_index(dims).to_xarray()['value'].rename(name)

    def to_dataset(self, *names: str) -> Any:
        """Variables as one :class:`xarray.Dataset`; all of them by default.

        Costs what it says: each variable arrives dense over its own dims,
        whatever the mask removed, and all of them at once. On a large model,
        name the few you need or use :meth:`to_parquet`.
        """
        assert self._executor._program is not None
        wanted = names or tuple(v.name for v in self._executor._program.variables)
        first, *rest = wanted
        dataset = self.to_dataarray(first).to_dataset(name=first)
        for name in rest:
            dataset[name] = self.to_dataarray(name)
        return dataset

    def to_parquet(self, directory: str | Path) -> dict[str, Path]:
        """One parquet file per variable, ``(dims…, value)``. Returns name → path.

        Sunk straight to disk, never copied into a second representation.
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
        """Load sources, create dim/parameter frames, variables, constraints.

        The compiler comes after the data, because two of its answers depend on
        it. Declarations are built one at a time and concatenated at the end:
        their rows are independent, which is what lets the model be four frames
        rather than a graph.
        """

        self._program = program

        for p in program.parameters:
            self._create_param_frame(p, sources)
        self._create_dim_frames(program, sources)

        self._compiler = PolarsCompiler(
            program,
            dict(self._dim_card),
            frozenset(self._bool_params),
            self._parameters,
            self._dimensions,
            self._variables,
        )

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
        """Bind one parameter's source and register it as a tidy frame."""
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
        # streaming here and nowhere else in this file: this is the one
        # collect whose result is model-sized, so it is the one the engine
        # choice moves. `collect()` defaults to the in-memory engine — unlike
        # `sink_csv`, whose default resolves to streaming — and switching every
        # collect costs 29% on a small join-heavy model to save the same 0.15 GB
        # this one saves alone.
        frame = frame.select(wanted).collect(engine='streaming').lazy()
        self._check_one_row_per_coordinate(p, frame)
        if frame.collect_schema()['value'] == pl.Boolean:
            self._bool_params.add(p.name)
        self._parameters[p.name] = frame

    def _check_one_row_per_coordinate(self, p: plan.ParameterDeclaration, frame: pl.LazyFrame) -> None:
        """A parameter is a function of its dims: one row per coordinate.

        Two rows for one has no defined meaning, and the eager lane refuses to
        lay such a source out at all, so naming it beats silently summing it.
        It also earns the assembly's skipped aggregate
        (:attr:`~farkas.relational.compiler.TermFragment.keyed`), for one pass
        over a source orders of magnitude smaller than the matrix.
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
        """Build every dimension's frame, then check its coordinates.

        A dimension with no explicit index has no declared order, so its labels
        are sorted. Containment runs once every frame exists: it stops a
        mistyped coordinate from vanishing in the join that places its terms,
        leaving a model that builds and solves without them.
        """
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
                stacked = pl.concat([self._parameters[p.name].select(pl.col(d).alias('val')) for p in params])
                table = stacked.unique().sort('val').with_row_index('ord').with_columns(pl.col('ord').cast(pl.Int64))
            materialised = table.collect()
            self._dimensions[d] = materialised.lazy()
            self._dim_card[d] = materialised.height

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
        """A dimension's ``(val, ord, coordinates…)`` from a caller's index.

        Ordinals follow the source's own order, so ``roll``/``shift`` moves by
        position exactly as the eager lane does even for string labels. A
        label's position is the row it first appears at.
        """
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

        Variables and constraint rows are the same operation, so it is written
        once. A label follows declaration order, which is what lets it *be* the
        solver's own index with no remapping.

        The mask chooses the path. **Unmasked**, every coordinate exists, so a
        row's label is its position in the product — arithmetic on the dim
        ordinals, with no sort and nothing to count. **Masked**, which rows
        survive is not known until the predicate has run, so the position has
        to be counted, and that costs a sort.

        Both return ``(dims…, label)`` in that column order. A mask that
        removes nothing has to be indistinguishable from no mask, down to the
        schema.
        """
        frame = self._q.frame(dims, where)
        if where is None:
            rows = math.prod(self._dim_card[d] for d in dims)
            return self._positional(frame, dims, label, start), start + rows

        import polars as pl

        materialised = (
            frame.sort([_ordinal(d) for d in dims])
            .select(*dims)
            .with_row_index(label, offset=start)
            .select(*dims, pl.col(label).cast(pl.Int64))
            .collect(engine='streaming')
        )
        return materialised, start + materialised.height

    def _positional(self, frame: pl.LazyFrame, dims: tuple[str, ...], label: str, start: int) -> pl.DataFrame:
        """Labels as row-major position in the coordinate product.

        The trailing dim has stride 1 and every other is the product of the
        cardinalities to its right, so the label is a dot product against the
        ordinals the frame already carries — no ordering imposed, because the
        answer does not depend on the order rows arrive in.
        """
        import polars as pl

        stride, position = 1, pl.lit(start, dtype=pl.Int64)
        for d in reversed(dims):
            position = position + pl.col(_ordinal(d)) * stride
            stride *= self._dim_card[d]
        return frame.select(*dims, position.alias(label)).collect(engine='streaming')

    # ------------------------------------------------------------------
    # declarations
    # ------------------------------------------------------------------

    def _build_variable(self, v: plan.VariableDeclaration) -> pl.DataFrame:
        """One variable's labelled frame, and its share of ``cols``."""
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
        """One constraint as its ``rows`` and its share of the matrix.

        Terms normalise to the left, constants to the right. Each constant
        fragment is aggregated to its own coordinates and left-joined, so a
        coordinate it has no row for contributes zero.

        The terminal aggregate is where duplicates from ``Sum`` and
        ``GroupSum`` — which project rather than aggregate — collapse, and it
        is skipped where nothing can (:func:`_needs_aggregate`).
        """
        import polars as pl

        if not c.dims:
            raise LanguageError(f"constraint '{c.name}' has no dims")
        lhs = self._q.expression(c.lhs, f"constraint '{c.name}' lhs")
        rhs = self._q.expression(c.rhs, f"constraint '{c.name}' rhs")
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
        stacked = pl.concat(pieces)
        if not _needs_aggregate([fragment for fragment, _ in terms]):
            return rows, stacked.collect(engine='streaming')

        # The aggregate is reachable, but "reachable" is all the fragments can
        # say. Sorting first turns the question into one pass over adjacent
        # pairs, and a hash table sized by the number of groups — which is
        # nearly the number of rows, since a repeated cell is the exception —
        # is only built when there is something to collapse.
        matrix = stacked.sort('row', 'col').collect(engine='streaming')
        if _has_repeated_entry(matrix):
            matrix = matrix.group_by('row', 'col').agg(pl.col('coeff').sum()).sort('row', 'col')
        return rows, matrix

    def _build_objective(self, o: plan.ObjectiveDeclaration) -> pl.DataFrame | None:
        """The objective as ``(col, coeff)``, or ``None`` if it has no terms.

        **This projection drops the dims, so it asks for the stronger key** —
        ``_needs_aggregate(..., projected=True)``. Where the matrix keeps a
        fragment's dims in ``row``, here only ``var_label`` survives, and a dim
        that arrived by broadcast then puts several rows on one column.

        Their sum is the coefficient, and nothing downstream computes it: the
        hand-off scatters with ``dense[at] = values``, which keeps the *last*
        write, and the LP writer emits one term per row for a reader to
        interpret as it likes. So a missed aggregate here is a wrong objective
        that still solves, not a slow one.
        """
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
        if _needs_aggregate(comp.terms, projected=True):
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
        batch_rows: int | None = None,
        solver_options: Mapping[str, Any] | None = None,
    ) -> Result:
        """Sink the built model straight into HiGHS and solve it.

        ``solver_options`` is forwarded verbatim to the solver, the way
        linopy's is — ``{'time_limit': 60, 'mip_rel_gap': 0.01}``.
        ``batch_rows`` is the hand-off budget in elements, and defaults to the
        sink's own — see :data:`~farkas.relational.sinks.highs.HANDOFF_BUDGET`.
        """
        status, objective, primal, dual = sinks.solve_direct(self._tables(), batch_rows, solver_options)
        self._sol, self._duals = primal, dual
        return Result(_status=status, _objective=objective, _executor=self, _has_duals=dual is not None)

    def _solution_frame(self, name: str) -> pl.LazyFrame:
        """The tidy solution of variable *name*: ``(dims…, value)``.

        A label join, never a dense array.
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
        """:meth:`_solution_frame` against row labels instead of column ones."""
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

        Integrality is decidable from the program, and naming the variable is
        actionable where "the solver reported none" is not.
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


def _needs_aggregate(terms: Sequence[TermFragment], *, projected: bool = False) -> bool:
    """Whether stacking *terms* can put two rows on one solver column.

    Named for the answer, not the condition: an inverted test here is a wrong
    model rather than a slow one.

    Two fragments may both carry the same variable — ``x + 2 * x`` is one row
    each and one column — and a single fragment that is not
    :attr:`~farkas.relational.compiler.TermFragment.keyed` already holds a
    label twice.

    *projected* is what the two call sites do not share. The matrix keeps a
    fragment's dims: a constraint's ``row`` is a function of dims that include
    them, so ``keyed`` — one row per ``(dims…, var_label)`` — carries straight
    into ``(row, col)``. The objective keeps only ``var_label``, so it has to
    ask the stronger question the shape operators already ask when they drop a
    dim: does the key survive losing *all* of them? It does exactly when
    ``var_label`` determines every dim the fragment still carries.

    That distinction is the whole bug this argument exists to prevent.
    ``p * cost`` is keyed on ``(snapshot, generator, var_label)`` and every one
    of those dims is the variable's own, so a column cannot repeat and the
    aggregate is dead weight. ``y * w`` — ``y`` over buses, ``w`` over
    snapshots — is just as keyed, but ``snapshot`` arrived by broadcast, so one
    column holds a row per snapshot and their *sum* is the coefficient.

    Worth 2-4x of build time on the matrix and little on the objective, but the
    argument is the same at both, so it is written once. On the duckdb engine
    the same change measured at nothing — the value is engine-specific even
    though the reasoning is not (#161).
    """
    if len(terms) != 1:
        return True
    (term,) = terms
    return not term.survives_dropping(set(term.dims) if projected else set())


def _has_repeated_entry(matrix: pl.DataFrame) -> bool:
    """Whether a matrix sorted by ``(row, col)`` holds one cell twice.

    :func:`_needs_aggregate` answers whether a stack *can* repeat a cell, which
    is all a static reading of the fragments can say. This answers whether it
    *did*, which is one pass over a sorted frame and lets the aggregate be
    skipped in the case the static answer is conservative about.

    That case is not rare. `transport` stacks three fragments, so the static
    answer is yes on every row, and at the `l` rung the aggregate collapses
    exactly nothing out of 12.6M entries.
    """
    import polars as pl

    if matrix.height < 2:
        return False
    repeated = (pl.col('row') == pl.col('row').shift(1)) & (pl.col('col') == pl.col('col').shift(1))
    return bool(matrix.select(repeated.any()).item())


def _stack(frames: list[pl.DataFrame], columns: tuple[str, ...]) -> pl.DataFrame:
    """Concatenate *frames*, or an empty frame of *columns* when there are none.

    Named rather than inferred because a model may legitimately have nothing to
    stack, and a sink still has to find what it reads.
    """
    import polars as pl

    if frames:
        return pl.concat(frames)
    return pl.DataFrame(schema={name: getattr(pl, _DTYPES[name]) for name in columns})
