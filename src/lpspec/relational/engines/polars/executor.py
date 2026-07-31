"""Polars executor: fill the model frames, then hand them to a sink.

Owns the *assembly* — turning each declaration into rows of the four model
frames, and holding them until a sink drains them. Owns none of the three
questions it asks on the way: what the data is
(:mod:`lpspec.relational.engines.polars.binding`), what a query over it looks like
(:mod:`lpspec.relational.engines.polars.compiler`), which coordinate gets which solver index
(:mod:`lpspec.relational.engines.polars.labels`). The lane is described in
docs/ARCHITECTURE.md.

The two registries it does own are the ones that fill *during* assembly — the
variable and constraint frames — because a declaration built later has to see
what earlier ones produced. Everything binding produced is frozen by contrast,
which is what :class:`~lpspec.relational.engines.polars.binding.BoundSources` says.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, get_args

import polars as pl

from lpspec.errors import (
    DataError,
    LanguageError,
    LpspecError,
    null_bounds_message,
    sparse_divisor_message,
)
from lpspec.relational import plan, sinks
from lpspec.relational.engines.polars.binding import BoundSources, bind
from lpspec.relational.engines.polars.compiler import PolarsCompiler, TermFragment
from lpspec.relational.engines.polars.labels import Labeller
from lpspec.relational.result import Result

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


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


#: Deprecated. The engine's failures are now split between
#: :class:`~lpspec.errors.LanguageError` (the program says something the
#: engine cannot build) and :class:`~lpspec.errors.DataError` (a source is
#: missing or the wrong shape). This alias is their common base, so an existing
#: ``except RelationalBuildError`` keeps catching everything it used to.
RelationalBuildError = LpspecError


class PolarsExecutor:
    """Build a :class:`Program` into polars frames, then sink it."""

    def __init__(self) -> None:
        self._program: plan.Program | None = None
        self._compiler: PolarsCompiler | None = None
        self._labels: Labeller | None = None
        self._bound: BoundSources | None = None
        self._variables: dict[str, pl.LazyFrame] = {}
        self._constraints: dict[str, pl.LazyFrame] = {}
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
        """Bind *sources*, then build every declaration into the model frames.

        The compiler comes after the binding, because two of its answers are
        read off the data — a dim's size and whether a parameter is boolean.
        Declarations are then built one at a time and concatenated at the end:
        their rows are independent, which is what lets the model be four frames
        rather than a graph.
        """

        self._program = program
        self._bound = bind(program, sources)
        # The variable frames are passed apart from the bound data on purpose:
        # they are the one registry still being filled, appearing as each
        # declaration is built so a constraint compiled afterwards can see them.
        self._compiler = PolarsCompiler(program, self._bound, self._variables)
        self._labels = Labeller(self._compiler, self._bound.cardinality, program)

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
        # `BoundSources` is frozen, so it cannot be emptied in place the way the
        # registries above can: what frees the bound frames is dropping every
        # reference to them, and the compiler holds one.
        self._bound = None
        self._compiler = None
        self._labels = None

    def __enter__(self) -> PolarsExecutor:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        self.close()
        return False


def _needs_aggregate(terms: Sequence[TermFragment], *, projected: bool = False) -> bool:
    """Whether stacking *terms* can put two rows on one solver column.

    Named for the answer, not the condition: an inverted test here is a wrong
    model rather than a slow one.

    Two things can put a label twice into the stack, and they are asked
    separately. A single fragment that is not
    :attr:`~lpspec.relational.engines.polars.compiler.TermFragment.keyed`
    already holds one twice on its own. Two fragments collide only if they
    carry the *same* variable — ``x + 2 * x`` is one row each and one column —
    because labels are dense and assigned a declaration at a time, so distinct
    variables cannot reach a shared one however either fragment was reshaped.

    That second half is what makes the ordinary multi-term constraint free.
    ``reserve_up + reserve_down <= p_max`` stacks two fragments, and reading
    only their count says the aggregate is reachable — which on the `fleet`
    rungs means sorting every nonzero in the model to collapse nothing.

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
    if any(not t.survives_dropping(set(t.dims) if projected else set()) for t in terms):
        return True
    carried = [t.variable for t in terms]
    return len(set(carried)) != len(carried)


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
    fragment carries :attr:`~lpspec.relational.engines.polars.compiler.TermFragment.presence`
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
