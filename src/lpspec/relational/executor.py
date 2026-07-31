"""Polars executor: fill the model frames, then hand them to a sink.

Owns the *data* — sources, dim frames, labels, the four model frames, and the
join that reads solution values back. Owns neither the query
(:mod:`lpspec.relational.compiler`) nor the exit (:mod:`.sinks`). The lane is
described in docs/ARCHITECTURE.md.

Labels are :mod:`lpspec.relational.labels`: ``var_label`` *is* the solver's
column index and ``row`` its row index, and the three ways of reaching one have
to agree integer for integer, which is a job of its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

import polars as pl

from lpspec.errors import (
    DataError,
    LanguageError,
    LinopyYamlError,
    null_bounds_message,
    sparse_divisor_message,
)
from lpspec.relational import data_validation, plan, sinks
from lpspec.relational.compiler import PolarsCompiler, TermFragment
from lpspec.relational.frames import as_frame
from lpspec.relational.labels import Labeller
from lpspec.relational.result import Result

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


#: The four frames a sink reads, as schemas. Stated here because the executor
#: is what fills them and an empty model still has to have them.
_COLS = ('col', 'lb', 'ub', 'vtype')
_OBJ = ('col', 'coeff')
_ROWS = ('row', 'sense', 'rhs')
_MATRIX = ('row', 'col', 'coeff')

#: The dtype of each of those columns. ``vtype`` is an ``Enum`` over the
#: variable types the plan declares, rather than a string: it holds one word
#: per column and the same handful of words for the whole model, so as a string
#: it stores that word once per row — 0.098 GB of the ``cols`` frame's 0.333 at
#: 9.8M columns, against 0.010 as an Enum. The Enum also makes the vocabulary
#: explicit, so a fourth variable type added to
#: :data:`~lpspec.relational.plan.VariableType` and not reaching here fails
#: where the column is built rather than in whichever sink first compares
#: against a name it does not know.
_DTYPES = {
    'col': pl.Int64, 'row': pl.Int64,
    'lb': pl.Float64, 'ub': pl.Float64, 'rhs': pl.Float64, 'coeff': pl.Float64,
    'sense': pl.String, 'vtype': pl.Enum(get_args(plan.VariableType)),
}  # fmt: skip


#: Scratch column carrying a source row's position while first-occurrence
#: order is computed. The spaces make it unrepresentable as a declared name, so
#: it cannot collide with a column the caller's index already has.
_ROW_POSITION = '__row position__'


#: Deprecated. The engine's failures are now split between
#: :class:`~lpspec.errors.LanguageError` (the program says something the
#: engine cannot build) and :class:`~lpspec.errors.DataError` (a source is
#: missing or the wrong shape). This alias is their common base, so an existing
#: ``except RelationalBuildError`` keeps catching everything it used to.
RelationalBuildError = LinopyYamlError


class PolarsExecutor:
    """Build a :class:`Program` into polars frames, then sink it."""

    def __init__(self) -> None:
        self._program: plan.Program | None = None
        self._compiler: PolarsCompiler | None = None
        self._labels: Labeller | None = None
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

        # Dimensions with an index of their own come first, so a parameter's
        # labels can be checked against them in the pass that binds it rather
        # than in a second one over the same rows. The rest are *derived* from
        # the parameters, so they cannot be built until those exist — and a
        # derived dimension has no strangers to find.
        self._create_sourced_dim_frames(program, sources)
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
        self._labels = Labeller(self._compiler, self._dim_card, program)

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

    @property
    def _label(self) -> Labeller:
        assert self._labels is not None, 'build() has not run'
        return self._labels

    # ------------------------------------------------------------------
    # sources
    # ------------------------------------------------------------------

    def _create_param_frame(self, p: plan.ParameterDeclaration, sources: Mapping[str, Any]) -> None:
        """Bind one parameter's source and register it as a tidy frame."""

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
        # before the cast, not after: a dictionary-encoded column compares on
        # its codes, and widening it to strings first doubles the check
        data_validation.check_one_row_per_coordinate(p, frame, self._dimensions)
        frame = _plain_strings(frame, p.dims)
        if frame.collect_schema()['value'] == pl.Boolean:
            self._bool_params.add(p.name)
        self._parameters[p.name] = frame

    def _check_no_undefined_divisor(self, name: str, matrix: pl.DataFrame, *expressions: plan.Expression) -> None:
        """A null coefficient means a divisor had no value where the model divided.

        Read off the assembled matrix rather than reasoned about from
        coordinates, because only the matrix knows which divisions *survived*.
        A quotient joins its divisor with a left join (:func:`_join_mul`), so a
        missing value leaves a null; if the row was masked out, or the numerator
        variable does not exist there, the term never reaches this frame and
        there is nothing to report.

        That is what keeps the refusal from becoming a wall. Sparse data is the
        ordinary case, and the question is not whether a divisor is dense — it
        is whether it is defined wherever the model actually divides by it.
        """
        if 'coeff' not in matrix.columns:
            return
        undefined = matrix.get_column('coeff').is_null().sum()
        if undefined:
            params = sorted(plan.divisor_parameters(*expressions))
            raise DataError(f'{name}: {sparse_divisor_message(", ".join(params), int(undefined))}')

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

    @staticmethod
    def _declared_dims(program: plan.Program) -> set[str]:
        dims: set[str] = set()
        for v in program.variables:
            dims.update(v.dims)
        for c in program.constraints:
            dims.update(c.dims)
        for p in program.parameters:
            dims.update(p.dims)
        return dims

    def _register_dim(self, d: str, table: pl.LazyFrame) -> None:
        materialised = table.collect()
        self._dimensions[d] = materialised.lazy()
        self._dim_card[d] = materialised.height

    def _create_sourced_dim_frames(self, program: plan.Program, sources: Mapping[str, Any]) -> None:
        """Every dimension carrying its own index, before any parameter binds."""
        for d in sorted(self._declared_dims(program)):
            if d in sources:
                coordinates = sorted(dict(program.dimension(d).coordinates))
                self._register_dim(d, self._explicit_dim_frame(d, sources[d], coordinates))

    def _create_dim_frames(self, program: plan.Program, sources: Mapping[str, Any]) -> None:
        """Build every dimension's frame, then check its coordinates.

        A dimension with no explicit index has no declared order, so its labels
        are sorted. Containment runs once every frame exists: it stops a
        mistyped coordinate from vanishing in the join that places its terms,
        leaving a model that builds and solves without them.
        """

        dims = self._declared_dims(program)
        for d in sorted(dims):
            if d in self._dimensions:  # built by `_create_sourced_dim_frames`
                continue
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
            self._register_dim(d, table)

        for d in sorted(dims):
            for cname, target in sorted(program.dimension(d).coordinates):
                if target not in self._dim_card:
                    raise DataError(
                        f"dimension '{d}' coordinate '{cname}' targets '{target}', which "
                        f'no declaration in this model uses, so it has no coordinate set '
                        f'to check against'
                    )
                data_validation.check_coordinate_containment(d, cname, target, self._dimensions)

    def _explicit_dim_frame(self, d: str, source: Any, names: list[str]) -> pl.LazyFrame:
        """A dimension's ``(val, ord, coordinates…)`` from a caller's index.

        Ordinals follow the source's own order, so ``roll``/``shift`` moves by
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

    # ------------------------------------------------------------------
    # declarations
    # ------------------------------------------------------------------

    def _build_variable(self, v: plan.VariableDeclaration) -> pl.DataFrame:
        """One variable's labelled frame, and its share of ``cols``."""

        labelled, self._n_cols = self._label.frame(v.dims, v.where, 'var_label', self._n_cols)
        self._variables[v.name] = labelled.lazy()

        bounded = self._q.bounds(labelled.lazy(), v)
        cols = bounded.select(
            pl.col('var_label').alias('col'),
            pl.col('lb').cast(pl.Float64),
            pl.col('ub').cast(pl.Float64),
            pl.lit(v.variable_type, dtype=_DTYPES['vtype']).alias('vtype'),
        ).collect(engine='streaming')

        bad = cols.filter(pl.col('lb').is_null() | pl.col('ub').is_null()).height
        if bad:
            raise DataError(null_bounds_message(v.name, bad))
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

        restrictions = _absence_restrictions([p for p, _ in terms])
        labelled, self._n_rows = self._label.frame(c.dims, c.where, 'row', self._n_rows, restrictions)
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
            matrix = stacked.collect(engine='streaming')
            self._check_no_undefined_divisor(f"constraint '{c.name}'", matrix, c.lhs, c.rhs)
            return rows, matrix

        # The aggregate is reachable, but "reachable" is all the fragments can
        # say. Sorting first turns the question into one pass over adjacent
        # pairs, and a hash table sized by the number of groups — which is
        # nearly the number of rows, since a repeated cell is the exception —
        # is only built when there is something to collapse.
        matrix = stacked.sort('row', 'col').collect(engine='streaming')
        self._check_no_undefined_divisor(f"constraint '{c.name}'", matrix, c.lhs, c.rhs)
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
        sink's own — see :data:`~lpspec.relational.sinks.highs.HANDOFF_BUDGET`.
        """
        status, objective, primal, dual = sinks.solve_direct(self._tables(), batch_rows, solver_options)
        return Result(
            _status=status,
            _objective=objective,
            _executor=self,
            _primal_values=primal,
            _dual_values=dual,
        )

    def _solution_frame(self, name: str, values: pl.DataFrame | None) -> pl.LazyFrame:
        """The tidy solution of variable *name*: ``(dims…, value)``.

        A label join, never a dense array. *values* is the solver's column
        vector, held by the :class:`Result` that asks — the labels are the
        build's and shared, the values are one solve's and are not.

        **Ordered by label**, which is the order the coordinates already have:
        a label *is* row-major position in the coordinate product, so sorting
        on it hands the caller back the model's own order rather than the
        order a hash join happened to finish in. Stated rather than inherited,
        because neither input is guaranteed sorted — a mask decides which rows
        of the product survive, not how they arrive.
        """
        assert self._program is not None
        assert values is not None, 'no solve has stored a primal'
        dims = self._program.variable(name).dims
        return (
            self._variables[name]
            .join(values.lazy(), left_on='var_label', right_on='col', how='inner')
            .sort('var_label')
            .select(*dims, 'value')
        )

    def _primal(self, name: str, values: pl.DataFrame | None) -> pl.DataFrame:
        return self._solution_frame(name, values).collect(engine='streaming')

    def _dual(self, name: str, values: pl.DataFrame) -> pl.DataFrame:
        """:meth:`_solution_frame` against row labels instead of column ones.

        Ordered the same way, for the same reason — a constraint row's label
        is its position in that constraint's coordinate product.
        """
        assert self._program is not None
        dims = self._program.constraint(name).dims
        return (
            self._constraints[name]
            .join(values.lazy(), on='row', how='inner')
            .sort('row')
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

    def _solution_to_parquet(self, directory: Path, values: pl.DataFrame | None) -> dict[str, Path]:
        assert self._program is not None
        directory.mkdir(parents=True, exist_ok=True)
        written: dict[str, Path] = {}
        for v in self._program.variables:
            out = directory / f'{v.name}.parquet'
            self._solution_frame(v.name, values).sink_parquet(out)
            written[v.name] = out
        return written

    # ------------------------------------------------------------------

    def close(self) -> None:
        """Drop the built model. Optional — see :class:`Result`."""
        self._cols = self._obj = self._rows = self._matrix = None
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
    :attr:`~lpspec.relational.compiler.TermFragment.keyed` already holds a
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
    if matrix.height < 2:
        return False
    repeated = (pl.col('row') == pl.col('row').shift(1)) & (pl.col('col') == pl.col('col').shift(1))
    return bool(matrix.select(repeated.any()).item())


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


def _stack(frames: list[pl.DataFrame], columns: tuple[str, ...]) -> pl.DataFrame:
    """Concatenate *frames*, or an empty frame of *columns* when there are none.

    Named rather than inferred because a model may legitimately have nothing to
    stack, and a sink still has to find what it reads.
    """
    if frames:
        return pl.concat(frames)
    return pl.DataFrame(schema={name: _DTYPES[name] for name in columns})


def _absence_restrictions(terms: Sequence[TermFragment]) -> list[tuple[tuple[str, ...], pl.LazyFrame]]:
    """The presence frames a constraint's rows have to be contained in.

    Absence propagates into a comparison and drops the row there (v1
    ``convention.rst`` §6 and §12): ``x + y >= 10`` is not ``x >= 10`` where
    ``y`` is masked, it is no constraint at all. A term whose variable is absent
    therefore restricts the row set rather than merely contributing nothing.

    Only *variable* absence counts. A sparse parameter is a compressed dense
    array whose missing rows mean a zero coefficient (SPEC §8), which is why the
    fragment carries :attr:`~lpspec.relational.compiler.TermFragment.presence`
    separately from its frame, and why this reads that rather than the frame.

    **A fragment with nothing to restrict is skipped**, and that is load-bearing
    rather than tidy: a restriction is data — which rows survive is unknown until
    the presence frames are read — so it costs ``Labeller.frame`` both of its
    arithmetic paths. An unmasked variable's presence is its whole coordinate
    product and would remove nothing, so it never gets to impose that cost.
    """
    out: list[tuple[tuple[str, ...], pl.LazyFrame]] = []
    for p in terms:
        if p.presence is None or not p.dims:
            continue
        # `presence_dims` is narrower than `dims` for an acyclic shift, whose
        # vacated edge lies along one dimension and is silent about the rest.
        out.append((p.presence_dims or p.dims, p.presence))
    return out
