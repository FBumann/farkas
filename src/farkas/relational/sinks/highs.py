"""The ``solver_direct`` sink: COO batches straight into HiGHS.

No float→text→parse round trip — that is the whole reason this exists beside
:mod:`~farkas.relational.sinks.lp_file`. Columns and rows arrive as numpy
slices of the model frames, in batches, so no single array ever holds the whole
model on this side of the API.

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

    import polars as pl

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
) -> tuple[SolveStatus, float, pl.DataFrame | None, pl.DataFrame | None]:
    """Feed the model to HiGHS and solve it.

    Returns ``(status, objective, primal, dual)``. ``primal`` is a
    ``(col, value)`` frame and ``dual`` a ``(row, value)`` one, both for the
    caller to join back to coordinates — the caller owns that mapping, which is
    what keeps reading results the same join whichever sink was used.

    Either can be ``None``, and for different reasons. No primal means the
    solve left nothing worth reading; HiGHS hands back a full-length vector of
    zeros regardless, and returning it would only make it reachable. No **dual**
    is narrower: a mixed-integer model has no dual solution at all, and neither
    does a run stopped short of a simplex basis. HiGHS says which, so this
    returns ``None`` rather than the zeros it would otherwise hand over.
    """
    import highspy
    import numpy as np
    import polars as pl

    inf = highspy.kHighsInf
    h = highspy.Highs()
    h.setOptionValue('output_flag', False)
    for option, value in (solver_options or {}).items():
        h.setOptionValue(option, value)

    empty_i = np.empty(0, dtype=np.int32)
    empty_f = np.empty(0, dtype=np.float64)
    columns = (
        model.cols.lazy()
        .join(model.obj.lazy(), on='col', how='left')
        .select('col', 'lb', 'ub', 'vtype', pl.col('coeff').fill_null(0.0).alias('cost'))
        .sort('col')
        .collect(engine='streaming')
    )
    for batch in columns.iter_slices(batch_rows):
        lb = np.nan_to_num(batch['lb'].to_numpy(), neginf=-inf, posinf=inf)
        ub = np.nan_to_num(batch['ub'].to_numpy(), neginf=-inf, posinf=inf)
        cost = batch['cost'].to_numpy()
        h.addCols(len(cost), cost, lb, ub, 0, empty_i, empty_i, empty_f)
        noncontinuous = np.flatnonzero(batch['vtype'].to_numpy() != 'continuous')
        if len(noncontinuous):
            cols_idx = batch['col'].to_numpy().astype(np.int32)[noncontinuous]
            integrality = np.full(len(noncontinuous), int(highspy.HighsVarType.kInteger), dtype=np.uint8)
            h.changeColsIntegrality(len(noncontinuous), cols_idx, integrality)

    ordered_rows = model.rows.sort('row')
    ordered_matrix = model.matrix.sort('row')
    for lo, hi in model.row_chunks(batch_rows):
        rows = ordered_rows.filter(pl.col('row').is_between(lo, hi, closed='left'))
        a = ordered_matrix.filter(pl.col('row').is_between(lo, hi, closed='left'))
        rhs = rows['rhs'].to_numpy()
        sense = rows['sense'].to_numpy()
        rlb = np.where(sense == '<=', -inf, rhs)
        rub = np.where(sense == '>=', inf, rhs)
        # where each row's run of nonzeros begins: the CSR start vector HiGHS
        # wants, read off a matrix already ordered by row
        starts = np.searchsorted(a['row'].to_numpy(), rows['row'].to_numpy()).astype(np.int32)
        h.addRows(
            len(rhs),
            rlb,
            rub,
            a.height,
            starts,
            a['col'].to_numpy().astype(np.int32),
            a['coeff'].to_numpy(),
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
        return status, float('nan'), None, None

    objective = h.getInfo().objective_function_value + model.objective_constant
    solution = h.getSolution()
    # solver output is one array per quantity, positionally indexed by the
    # solver's own index — which *is* our label, densely assigned. So the join
    # column is an arange rather than anything read back.
    primal = _labelled('col', model.column_count, solution.col_value)
    dual = _labelled('row', model.row_count, solution.row_dual) if solution.dual_valid else None
    return status, objective, primal, dual


def _labelled(label: str, count: int, values: Any) -> pl.DataFrame:
    """``(label, value)`` over the solver's own dense index."""
    import numpy as np
    import polars as pl

    return pl.DataFrame({label: np.arange(count, dtype=np.int64), 'value': np.asarray(values, dtype=np.float64)})
