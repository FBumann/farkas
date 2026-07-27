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
    batch_rows: int = 100_000,
    solver_options: Mapping[str, Any] | None = None,
) -> tuple[SolveStatus, float]:
    """Stream the model into HiGHS and solve it. Returns ``(status, objective)``.

    On a solve that left values worth reading, the primal lands in a ``sol``
    table on the connection, so reading results back stays a label join like
    every other read — the caller owns the mapping from solver column index to
    coordinates. On any other outcome there is nothing to store: HiGHS still
    hands back a full-length vector of zeros, and keeping it would only make it
    reachable.
    """
    import highspy
    import numpy as np

    con = model.connection
    inf = highspy.kHighsInf
    h = highspy.Highs()
    h.setOptionValue('output_flag', False)
    for option, value in (solver_options or {}).items():
        h.setOptionValue(option, value)

    empty_i = np.empty(0, dtype=np.int32)
    empty_f = np.empty(0, dtype=np.float64)
    reader = con.execute(
        'SELECT c.col, c.lb, c.ub, c.vtype, COALESCE(o.coeff, 0) AS cost '
        'FROM cols c LEFT JOIN obj o USING (col) ORDER BY c.col'
    ).to_arrow_reader(batch_rows)
    for batch in reader:
        d = batch.to_pydict()
        lb = np.nan_to_num(np.asarray(d['lb'], dtype=np.float64), neginf=-inf, posinf=inf)
        ub = np.nan_to_num(np.asarray(d['ub'], dtype=np.float64), neginf=-inf, posinf=inf)
        cost = np.asarray(d['cost'], dtype=np.float64)
        h.addCols(len(cost), cost, lb, ub, 0, empty_i, empty_i, empty_f)
        variable_type = np.asarray(d['vtype'])
        noncontinuous = np.flatnonzero(variable_type != 'continuous')
        if len(noncontinuous):
            cols_idx = np.asarray(d['col'], dtype=np.int32)[noncontinuous]
            integrality = np.full(len(noncontinuous), int(highspy.HighsVarType.kInteger), dtype=np.uint8)
            h.changeColsIntegrality(len(noncontinuous), cols_idx, integrality)

    for lo, hi in model.row_chunks(batch_rows):
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
        return status, float('nan')

    objective = h.getInfo().objective_function_value + model.objective_constant

    import pyarrow as pa

    primal = pa.table(
        {
            'col': pa.array(np.arange(model.column_count, dtype=np.int64)),
            'value': pa.array(np.asarray(h.getSolution().col_value, dtype=np.float64)),
        }
    )
    con.execute('DROP TABLE IF EXISTS sol')
    con.register('sol_src', primal)
    con.execute('CREATE TABLE sol AS SELECT * FROM sol_src')
    con.unregister('sol_src')
    return status, objective
