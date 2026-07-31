"""The ``gurobi`` solver: COO blocks straight into gurobipy.

The same hand-off as :mod:`~lpspec.relational.sinks.solvers.highs`, reading
the same ``dense_columns``, so the two cannot disagree about the model they
load. Two things differ:

- **The matrix's currency.** HiGHS takes the three CSR arrays; gurobipy's
  matrix API takes a matrix *object*, so a block is wrapped in a
  ``scipy.sparse.csr_matrix`` over them — a view, not a copy. That wrapper is
  why the ``[gurobi]`` extra carries scipy: the alternative is a Python call
  per row.
- **Columns arrive in one call.** ``addMConstr`` writes into one ``MVar``
  spanning the model, so only the matrix is chunked.

``gurobipy`` and ``scipy`` are imported inside the functions, so importing
this module stays free for a caller who never solves with it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import polars as pl

from lpspec.relational.status import SolveStatus

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lpspec.relational.sinks.tables import ModelTables


#: Nonzeros per row block, spent through :mod:`~lpspec.relational.chunking`.
#: Not independently tuned — the measurement is
#: :data:`~lpspec.relational.sinks.solvers.highs.HANDOFF_BUDGET`'s, and holds
#: because a block is the same filter over the same sorted frame; wrapping
#: three arrays in a CSR view is constant work on top.
HANDOFF_BUDGET = 100_000

#: Gurobi status -> termination condition. Copied from linopy's own
#: ``Gurobi.CONDITION_MAP`` bar three entries (:data:`_LINOPY_DIVERGENCES`);
#: ``tests/test_solve_status.py`` asserts both halves, so linopy moving — or
#: fixing — fails here rather than silently.
_CONDITION_OF_GUROBI_STATUS = {
    1: 'unknown',
    2: 'optimal',
    3: 'infeasible',
    4: 'infeasible_or_unbounded',
    5: 'unbounded',
    6: 'other',
    7: 'iteration_limit',
    8: 'terminated_by_limit',
    9: 'time_limit',
    10: 'terminated_by_limit',
    11: 'user_interrupt',
    12: 'other',
    13: 'suboptimal',
    14: 'unknown',
    15: 'terminated_by_limit',
    16: 'terminated_by_limit',
    17: 'resource_interrupt',
}

#: Where the table above does not copy linopy's, and why: each contradicts a
#: status Gurobi documents, so copying it would import a wrong answer rather
#: than a shared vocabulary — the trade
#: :attr:`~lpspec.relational.status.SolveStatus.is_readable` already refused
#: once. The words stay linopy's; only the verdicts differ.
_LINOPY_DIVERGENCES = {
    10: 'SOLUTION_LIMIT stopped early after n incumbents; linopy calls it optimal',
    16: 'WORK_LIMIT is a limit, not a solver failure; linopy calls it internal_solver_error',
    17: 'MEM_LIMIT is the resource_interrupt linopy itself maps kMemoryLimit to on HiGHS',
}


def build_gurobi(
    model: ModelTables,
    batch_rows: int | None = None,
    solver_options: Mapping[str, Any] | None = None,
) -> Any:
    """Load the model into a :class:`gurobipy.Model` and stop there.

    :func:`~lpspec.relational.sinks.solvers.highs.build_highs`'s seam, drawn
    for its reason: the search is the same work whoever filled the model.
    ``batch_rows`` is the budget in *nonzeros*, and stays a parameter so tests
    can force ragged blocks.
    """
    return _load(model, batch_rows, solver_options)[0]


def solve_gurobi(
    model: ModelTables,
    batch_rows: int | None = None,
    solver_options: Mapping[str, Any] | None = None,
) -> tuple[SolveStatus, float, pl.DataFrame | None, pl.DataFrame | None]:
    """Feed the model to Gurobi and solve it.

    The family's shape, and the two ``None`` cases mean what they mean in
    :func:`~lpspec.relational.sinks.solvers.highs.solve_highs`: no primal, no
    dual. Gurobi refuses the attribute in both cases rather than handing back
    zeros, which is the one place it makes this easier than HiGHS.
    """
    m, x, blocks = _load(model, batch_rows, solver_options)
    m.optimize()

    status = _status_of(m)
    if not status.is_readable:
        return status, float('nan'), None, None

    return status, m.ObjVal, _labelled('col', x.X), _duals(blocks)


def _load(
    model: ModelTables,
    batch_rows: int | None,
    solver_options: Mapping[str, Any] | None,
) -> tuple[Any, Any, list[Any]]:
    """The model in a ``gurobipy.Model``, with the handles to read it back.

    ``x.X`` and ``block.Pi`` are numpy arrays; ``getVars()`` / ``getConstrs()``
    would build one Python object per column and row to reach the same numbers.
    The environment is the model's and dies with it. ``OutputFlag`` is an
    *environment* parameter because that is what suppresses the start-up
    banner; ``solver_options`` lands after, and so can put the log back.
    """
    gurobipy = _gurobipy()
    import numpy as np
    import scipy.sparse

    batch = HANDOFF_BUDGET if batch_rows is None else batch_rows
    m = gurobipy.Model(env=gurobipy.Env(params={'OutputFlag': 0}))
    for option, value in (solver_options or {}).items():
        m.setParam(option, value)

    lb, ub, cost, integral = model.dense_columns(gurobipy.GRB.INFINITY)
    x = m.addMVar(model.column_count, lb=lb, ub=ub, obj=cost, vtype=np.where(integral, 'I', 'C'))

    ordered_rows = model.rows.sort('row')
    ordered_matrix = model.matrix.sort('row')
    senses = np.array([gurobipy.GRB.LESS_EQUAL, gurobipy.GRB.GREATER_EQUAL, gurobipy.GRB.EQUAL], dtype='<U1')
    blocks = []
    for lo, hi in model.row_chunks_by_nonzeros(batch):
        rows = ordered_rows.filter(pl.col('row').is_between(lo, hi, closed='left')).select(
            'row',
            pl.col('sense').replace_strict({'<=': 0, '>=': 1, '==': 2}, return_dtype=pl.UInt8).alias('op'),
            'rhs',
        )
        a = ordered_matrix.filter(pl.col('row').is_between(lo, hi, closed='left'))
        starts = np.searchsorted(a['row'].to_numpy(), rows['row'].to_numpy())
        block = scipy.sparse.csr_matrix(
            (a['coeff'].to_numpy(), a['col'].to_numpy(), np.append(starts, a.height)),
            shape=(rows.height, model.column_count),
        )
        blocks.append(m.addMConstr(block, x, senses[rows['op'].to_numpy()], rows['rhs'].to_numpy()))

    if model.objective_sense == 'max':
        m.ModelSense = gurobipy.GRB.MAXIMIZE
    m.ObjCon = model.objective_constant
    m.update()
    return m, x, blocks


def _gurobipy() -> Any:
    """The optional dependency, or a message naming the extra. Both halves are
    named, because the missing one is as often scipy."""
    try:
        import gurobipy
        import scipy.sparse  # noqa: F401 — guarded here so the message covers it
    except ModuleNotFoundError as exc:
        msg = 'The gurobi sink requires the [gurobi] extra (gurobipy, scipy): pip install "lpspec[gurobi]"'
        raise ModuleNotFoundError(msg) from exc
    return gurobipy


def _status_of(m: Any) -> SolveStatus:
    """What the solve concluded, on both axes. ``SolCount`` answers "is there
    anything here", which the termination condition does not: a run stopped at
    a limit may or may not hold an incumbent."""
    code = int(m.Status)
    return SolveStatus(
        termination_condition=_CONDITION_OF_GUROBI_STATUS.get(code, 'unknown'),
        solver_wording=_wording(code),
        has_primal=m.SolCount > 0,
    )


def _wording(code: int) -> str:
    """Gurobi's own name for a status code, read off ``GRB.Status`` rather than
    tabulated — so one this package has never heard of still arrives
    searchable."""
    gurobipy = _gurobipy()
    names = {getattr(gurobipy.GRB.Status, name): name for name in dir(gurobipy.GRB.Status) if not name.startswith('_')}
    return names.get(code, str(code))


def _duals(blocks: list[Any]) -> pl.DataFrame | None:
    """Shadow prices in row order, or ``None`` where the model has none.

    Blocks were added in ascending row ranges, so concatenating their slices
    reproduces the row index without a sort. Gurobi refuses ``Pi`` on a
    mixed-integer model, and that refusal *is* the answer — no zero vector to
    test.
    """
    import numpy as np

    gurobipy = _gurobipy()
    try:
        values = [block.Pi for block in blocks]
    except (AttributeError, gurobipy.GurobiError):
        return None
    return _labelled('row', np.concatenate(values) if values else np.empty(0, dtype=np.float64))


def _labelled(label: str, values: Any) -> pl.DataFrame:
    """``(label, value)`` over the solver's own dense index, which *is* our
    label — so the join column is an ``arange``, not something read back."""
    import numpy as np

    values = np.asarray(values, dtype=np.float64)
    return pl.DataFrame({label: np.arange(len(values), dtype=np.int64), 'value': values})
