"""The ``solver_direct`` sink: COO batches straight into HiGHS.

No float→text→parse round trip — that is the whole reason this exists beside
:mod:`~farkas.relational.sinks.lp_file`. Columns arrive as arrow batches,
rows as numpy slices of ``A``, and the full model never lands in one array.

``highspy`` is imported inside the function rather than at module scope: it is
an optional dependency, and importing this module must stay free for callers
that only ever write LP files. The module boundary is the fence; the lazy
import is what keeps the fence cheap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from farkas.relational.status import SolveStatus

if TYPE_CHECKING:
    from collections.abc import Mapping

    from farkas.relational.sinks.tables import ModelTables


#: HiGHS model status -> termination condition. Copied from linopy's own
#: ``Highs.CONDITION_MAP``; ``tests/test_solve_status.py`` asserts it still
#: matches, so a HiGHS release that adds a status shows up as a failure here
#: rather than as a silent ``unknown``.
_CONDITION_OF_HIGHS_STATUS = {
    'kNotset': 'unknown',
    'kLoadError': 'internal_solver_error',
    'kModelError': 'internal_solver_error',
    'kPresolveError': 'internal_solver_error',
    'kSolveError': 'internal_solver_error',
    'kPostsolveError': 'internal_solver_error',
    'kModelEmpty': 'unknown',
    'kMemoryLimit': 'resource_interrupt',
    'kOptimal': 'optimal',
    'kInfeasible': 'infeasible',
    'kUnboundedOrInfeasible': 'infeasible_or_unbounded',
    'kUnbounded': 'unbounded',
    'kObjectiveBound': 'terminated_by_limit',
    'kObjectiveTarget': 'terminated_by_limit',
    'kTimeLimit': 'time_limit',
    'kIterationLimit': 'iteration_limit',
    'kSolutionLimit': 'terminated_by_limit',
    'kInterrupt': 'user_interrupt',
    'kUnknown': 'unknown',
}


def solve_direct(
    model: ModelTables,
    batch_rows: int | None = None,
    solver_options: Mapping[str, Any] | None = None,
) -> tuple[SolveStatus, float, bool]:
    """Stream the model into HiGHS and solve it.

    ``batch_rows`` defaults to the executor's ``chunk_rows`` — the budget that
    already governs every other batched pass in the engine. It was a separate
    2e6-vs-1e5 default, which made the sink twenty times finer-grained than
    the thing it reads from for no stated reason: at 10M columns that is 2.59s
    against 1.65s, for 4% more peak once HiGHS's own model is resident. The
    parameter stays so tests can force ragged chunks.

    Returns ``(status, objective, has_duals)``. On a solve that left values
    worth reading, the primal lands in a ``sol`` table on the connection — and
    the duals in a ``dual`` table when HiGHS produced valid ones — so reading
    results back stays a label join like every other read: the caller owns the
    mapping from solver index to coordinates. On any other outcome there is
    nothing to store: HiGHS still hands back a full-length vector of zeros, and
    keeping it would only make it reachable.

    ``has_duals`` is the sink's verdict rather than the caller's guess. A MIP
    has no dual solution at all, and neither does a run stopped short of a
    simplex basis — both are ``dual_valid = False``, and in both the table is
    absent rather than zero-filled.
    """
    import highspy
    import numpy as np

    batch = model.chunk_rows if batch_rows is None else batch_rows
    con = model.connection
    inf = highspy.kHighsInf
    h = highspy.Highs()
    h.setOptionValue('output_flag', False)
    for option, value in (solver_options or {}).items():
        h.setOptionValue(option, value)

    empty_i = np.empty(0, dtype=np.int32)
    empty_f = np.empty(0, dtype=np.float64)
    # Columns arrive in bounded ranges, not as one ordered stream. `addCols`
    # appends, so column k must be the k-th row we hand over — but ordering the
    # whole table to get that is a *global* sort, and duckdb's sort is the
    # operator that does not reliably stay inside `memory_limit` (benchmarks.md
    # operational finding 1). `to_arrow_reader` batches the delivery, not the
    # sort, so it does not help: the ordering happens before the first batch.
    # Ranges give the same order with each sort bounded by the chunk, and the
    # filters prune, since both tables are stored ascending in their label.
    for lo, hi in model.col_chunks(batch):
        reader = con.execute(
            f'SELECT c.col, c.lb, c.ub, c.vtype, COALESCE(o.coeff, 0) AS cost '
            f'FROM cols c LEFT JOIN obj o USING (col) '
            f'WHERE c.col >= {lo} AND c.col < {hi} ORDER BY c.col'
        ).to_arrow_reader(batch)
        for arrow_batch in reader:
            _add_cols(h, highspy, np, arrow_batch, inf, empty_i, empty_f)

    # by nonzeros, not by rows: this loop's residency is the slice of `A` it
    # fetches, and a range of rows says nothing about how many entries that is
    for lo, hi in model.row_chunks_by_nonzeros(batch):
        rows = con.execute(
            f'SELECT row, sense, rhs FROM rows WHERE row >= {lo} AND row < {hi} ORDER BY row'
        ).fetchnumpy()
        a = con.execute(f'SELECT row, col, coeff FROM A WHERE row >= {lo} AND row < {hi} ORDER BY row').fetchnumpy()
        rhs = np.asarray(rows['rhs'], dtype=np.float64)
        sense = rows['sense']
        rlb = np.where(sense == '<=', -inf, rhs)
        rub = np.where(sense == '>=', inf, rhs)
        starts = np.searchsorted(np.asarray(a['row']), np.asarray(rows['row'])).astype(np.int32)
        h.addRows(
            len(rhs),
            rlb,
            rub,
            len(a['col']),
            starts,
            np.asarray(a['col'], dtype=np.int32),
            np.asarray(a['coeff'], dtype=np.float64),
        )

    if model.objective_sense == 'max':
        h.changeObjectiveSense(highspy.ObjSense.kMaximize)
    h.run()

    highs_status = str(h.getModelStatus()).rsplit('.', 1)[-1]
    status = SolveStatus(
        termination_condition=_CONDITION_OF_HIGHS_STATUS.get(highs_status, 'unknown'),
        solver_wording=h.modelStatusToString(h.getModelStatus()),
        # the solver's own answer to "is there a primal here", which the
        # termination condition does not give: a run stopped at a time limit
        # may or may not have found an incumbent
        has_primal=h.getInfo().primal_solution_status == int(highspy.SolutionStatus.kSolutionStatusFeasible),
    )
    if not status.is_readable:
        # linopy's convention, and an honest one: nan is a sentinel that
        # propagates through a scenario sweep, where 0.0 reads as an answer
        return status, float('nan'), False

    objective = h.getInfo().objective_function_value + model.objective_constant

    solution = h.getSolution()
    _store(con, 'sol', 'col', model.column_count, solution.col_value)

    # Same bargain as the primal above, one quantity down: HiGHS says whether a
    # dual solution exists, so drop the table when it does not rather than
    # storing the zeros it hands back regardless.
    con.execute('DROP TABLE IF EXISTS dual')
    has_duals = bool(solution.dual_valid)
    if has_duals:
        _store(con, 'dual', 'row', model.row_count, solution.row_dual)

    return status, objective, has_duals


def _store(con: Any, table: str, label: str, count: int, values: Any) -> None:
    """Materialise *values* as ``(label, value)``, densely labelled ``0..count``.

    Solver output arrives as one array per quantity, positionally indexed by
    the solver's own index — which *is* our label, densely assigned. So the
    join column is an ``arange`` rather than anything read back.
    """
    import numpy as np
    import pyarrow as pa

    source = pa.table(
        {
            label: pa.array(np.arange(count, dtype=np.int64)),
            'value': pa.array(np.asarray(values, dtype=np.float64)),
        }
    )
    con.execute(f'DROP TABLE IF EXISTS {table}')
    con.register(f'{table}_src', source)
    con.execute(f'CREATE TABLE {table} AS SELECT * FROM {table}_src')
    con.unregister(f'{table}_src')


def _add_cols(
    h: Any,
    highspy: Any,
    np: Any,
    batch: Any,
    inf: float,
    empty_i: Any,
    empty_f: Any,
) -> None:
    """One Arrow batch of ``(col, lb, ub, vtype, cost)`` into HiGHS.

    Arrow goes to numpy directly, never through ``to_pydict``. That call
    materialises every value as a Python object before numpy ever sees it —
    five columns times ten million rows is fifty million boxed floats, and it
    cost more than everything else in this sink combined (16.02 s of an 18.03 s
    column loop at 10M columns; 0.31 s here). Nothing else about the loop
    changed to get that back.

    Split out only because the column loop has two levels — a range and the
    batches inside it — and burying the numpy handling under both made the
    chunking hard to see. ``highspy`` and ``np`` are passed rather than
    imported: both are lazy at the call site, and importing them here would put
    them back at module scope by the back door.
    """

    def column(name: str) -> Any:
        # zero_copy_only=False: bounds carry infinities and vtype is a string
        # column, so neither is guaranteed to be a borrowable buffer.
        return batch.column(name).to_numpy(zero_copy_only=False)

    lb = np.nan_to_num(column('lb'), neginf=-inf, posinf=inf)
    ub = np.nan_to_num(column('ub'), neginf=-inf, posinf=inf)
    cost = np.asarray(column('cost'), dtype=np.float64)
    h.addCols(len(cost), cost, lb, ub, 0, empty_i, empty_i, empty_f)
    noncontinuous = np.flatnonzero(column('vtype') != 'continuous')
    if len(noncontinuous):
        cols_idx = column('col').astype(np.int32)[noncontinuous]
        integrality = np.full(len(noncontinuous), int(highspy.HighsVarType.kInteger), dtype=np.uint8)
        h.changeColsIntegrality(len(noncontinuous), cols_idx, integrality)
