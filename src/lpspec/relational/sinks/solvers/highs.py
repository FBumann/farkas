"""The ``highs`` solver: COO batches straight into HiGHS.

The default, and the only one whose dependency ships with the package. No
float→text→parse round trip — that is why this exists beside
:mod:`~lpspec.relational.sinks.writers.lp_file`. Columns and rows arrive as
numpy slices, in batches.

**Nothing textual crosses into numpy.** A polars ``String`` column converts by
boxing every value as a Python object, so a comparison against ``'<='`` is made
in polars and only the bound it selects is handed over — the same rule
:meth:`~lpspec.relational.sinks.tables.ModelTables.dense_columns` states for
the column vectors, where it was measured.

``highspy`` is imported inside the function: it is an optional dependency, and
importing this module must stay free for callers that only write LP files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import polars as pl

from lpspec.errors import LpspecError
from lpspec.relational.status import SolveStatus

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lpspec.relational.sinks.tables import ModelTables


#: Elements per hand-off chunk. The engine has one batched pass left — this
#: one — because labels became positional, so this is the sink's own budget
#: rather than a copy of a build-side knob. Spent through
#: :mod:`~lpspec.relational.chunking`, which asks a caller to state what one
#: unit costs: a column is one element, a constraint row is as many as it has
#: nonzeros.
#:
#: Deliberately small, and the opposite of what the same budget settled at on
#: the duckdb engine. There a wider chunk was worth a third of the hand-off,
#: because every chunk re-ran an ordered query; here the columns are numpy
#: slices and the rows a filter over an already-sorted frame, so more chunks
#: cost almost nothing and only residency scales with the budget. Measured at
#: `l`: 2e6 against 1e5 is 0.50s against 0.59s on `dispatch` and 0.71 against
#: 0.75 on `transport`, for 0.6 GB and 0.2 GB more peak. A tenth of a second
#: on a hand-off that precedes a minute of simplex is not worth half a
#: gigabyte of the invariant.
HANDOFF_BUDGET = 100_000

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


def build_highs(
    model: ModelTables,
    batch_rows: int | None = None,
    solver_options: Mapping[str, Any] | None = None,
) -> Any:
    """Load the model into a :class:`highspy.Highs` and stop there.

    The hand-off without the simplex. :func:`solve_highs` is this plus
    ``run()``, and the seam exists because the two are different questions: the
    simplex is the same work whoever filled the model, so a measurement that
    includes it says nothing about the lane that filled it. `bench/` ends here,
    and linopy's ``Model.to_highspy()`` is the same seam on that side.

    ``batch_rows`` is the budget in *elements*, spent through
    :mod:`~lpspec.relational.chunking` so a chunk's width is stated rather than
    assumed. The parameter stays so tests can force ragged chunks.
    """
    import highspy
    import numpy as np

    batch = HANDOFF_BUDGET if batch_rows is None else batch_rows
    inf = highspy.kHighsInf
    h = highspy.Highs()
    h.setOptionValue('output_flag', False)
    for option, value in (solver_options or {}).items():
        h.setOptionValue(option, value)

    empty_i = np.empty(0, dtype=np.int32)
    empty_f = np.empty(0, dtype=np.float64)
    lb, ub, cost, integral = model.dense_columns(inf)
    for lo, hi in model.col_chunks(batch):
        _loaded(h, h.addCols(hi - lo, cost[lo:hi], lb[lo:hi], ub[lo:hi], 0, empty_i, empty_i, empty_f), 'columns')
        noncontinuous = np.flatnonzero(integral[lo:hi]).astype(np.int32) + np.int32(lo)
        if len(noncontinuous):
            integrality = np.full(len(noncontinuous), int(highspy.HighsVarType.kInteger), dtype=np.uint8)
            h.changeColsIntegrality(len(noncontinuous), noncontinuous, integrality)

    ordered_rows = model.rows.sort('row')
    ordered_matrix = model.matrix.sort('row')
    for lo, hi in model.row_chunks_by_nonzeros(batch):
        rows = ordered_rows.filter(pl.col('row').is_between(lo, hi, closed='left')).select(
            'row',
            pl.when(pl.col('sense') == '<=').then(-inf).otherwise(pl.col('rhs')).alias('rlb'),
            pl.when(pl.col('sense') == '>=').then(inf).otherwise(pl.col('rhs')).alias('rub'),
        )
        a = ordered_matrix.filter(pl.col('row').is_between(lo, hi, closed='left'))
        rlb = rows['rlb'].to_numpy()
        rub = rows['rub'].to_numpy()
        starts = np.searchsorted(a['row'].to_numpy(), rows['row'].to_numpy()).astype(np.int32)
        _loaded(
            h,
            h.addRows(
                len(rlb),
                rlb,
                rub,
                a.height,
                starts,
                a['col'].to_numpy().astype(np.int32),
                a['coeff'].to_numpy(),
            ),
            'rows',
        )

    if model.objective_sense == 'max':
        h.changeObjectiveSense(highspy.ObjSense.kMaximize)
    return h


def solve_highs(
    model: ModelTables,
    batch_rows: int | None = None,
    solver_options: Mapping[str, Any] | None = None,
) -> tuple[SolveStatus, float, pl.DataFrame | None, pl.DataFrame | None]:
    """Feed the model to HiGHS and solve it.

    Returns ``(status, objective, primal, dual)`` as ``(col, value)`` and
    ``(row, value)`` frames for the caller to join back to coordinates.

    Either can be ``None``, for different reasons. No primal means the solve
    left nothing worth reading. No dual is narrower: a mixed-integer model has
    none at all, and neither does a run stopped short of a simplex basis. HiGHS
    hands back full-length vectors of zeros either way, and returning them
    would only make them reachable.
    """
    import highspy

    h = build_highs(model, batch_rows, solver_options)
    h.run()

    status = _status_of(h, highspy)
    if not status.is_readable:
        return status, float('nan'), None, None

    objective = h.getInfo().objective_function_value + model.objective_constant
    solution = h.getSolution()
    primal = _labelled('col', model.column_count, solution.col_value)
    dual = _labelled('row', model.row_count, solution.row_dual) if solution.dual_valid else None
    return status, objective, primal, dual


def _loaded(h: Any, status: Any, what: str) -> None:
    """Raise unless the solver accepted the batch.

    HiGHS reports a rejected batch by return value and carries on with an empty
    model, so an unchecked call turns a malformed handoff into a confident
    answer to a different problem — an unconstrained one, if it was the rows
    that were refused.
    """
    import highspy

    if status == highspy.HighsStatus.kError:
        raise LpspecError(
            f'the solver refused a batch of {what}: {h.modelStatusToString(h.getModelStatus())!r}. '
            f'Nothing was loaded, so any answer would describe a different model. '
            f'This is an engine bug rather than a problem with the model — please report it.'
        )


def _status_of(h: Any, highspy: Any) -> SolveStatus:
    """What the solve concluded, on both axes.

    ``has_primal`` is the solver's own answer to "is there anything here",
    which the termination condition does not give: a run stopped at a time
    limit may or may not have found an incumbent.
    """
    model_status = h.getModelStatus()
    return SolveStatus(
        termination_condition=_CONDITION_OF_HIGHS_STATUS.get(str(model_status).rsplit('.', 1)[-1], 'unknown'),
        solver_wording=h.modelStatusToString(model_status),
        has_primal=h.getInfo().primal_solution_status == int(highspy.SolutionStatus.kSolutionStatusFeasible),
    )


def _labelled(label: str, count: int, values: Any) -> pl.DataFrame:
    """``(label, value)`` over the solver's own dense index.

    Solver output is one array per quantity, positionally indexed by the
    solver's index — which *is* our label, densely assigned — so the join
    column is an ``arange`` rather than anything read back.
    """
    import numpy as np

    return pl.DataFrame({label: np.arange(count, dtype=np.int64), 'value': np.asarray(values, dtype=np.float64)})
