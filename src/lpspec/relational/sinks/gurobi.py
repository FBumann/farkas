"""The ``gurobi`` sink: COO blocks straight into gurobipy.

The second solver a built model can be handed to, and deliberately the same
hand-off as :mod:`~lpspec.relational.sinks.highs` — dense column vectors and
CSR row blocks, no float→text→parse round trip. Both read
:meth:`~lpspec.relational.sinks.tables.ModelTables.dense_columns` for the
columns, so the two sinks cannot disagree about the model they load.

**What differs is the currency of the matrix.** HiGHS takes the three CSR
arrays as arguments; gurobipy's matrix API takes a matrix *object*, so each
block is wrapped in a ``scipy.sparse.csr_matrix`` over the arrays the labels
already laid out — a view, not a second copy. That wrapper is why the
``[gurobi]`` extra carries scipy: the alternative bulk path is a Python call
per row.

**Columns arrive in one call, rows in blocks.** ``addMConstr`` writes into one
``MVar`` spanning the whole model, so there is no column batching to do here
the way :func:`~lpspec.relational.sinks.highs.build_highs` does it; the dense
vectors are the same size either way, and only the matrix is chunked.

``gurobipy`` and ``scipy`` are imported inside the functions: both are
optional, and importing this module must stay free for a caller that solves
with HiGHS or only writes LP files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import polars as pl

from lpspec.relational.status import SolveStatus

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lpspec.relational.sinks.tables import ModelTables


#: Nonzeros per row block, spent through :mod:`~lpspec.relational.chunking`.
#: The same number as the HiGHS sink's and for the same reason — a block is a
#: filter over an already-sorted frame, so more blocks cost almost nothing and
#: only residency scales with the budget. It is not independently tuned: the
#: measurement that settled it is
#: :data:`~lpspec.relational.sinks.highs.HANDOFF_BUDGET`'s, and the one thing
#: this sink adds per block — wrapping three arrays in a CSR view — is
#: constant work.
HANDOFF_BUDGET = 100_000

#: Gurobi optimization status -> termination condition. Copied from linopy's
#: own ``Gurobi.CONDITION_MAP``, with three entries deliberately not copied
#: (:data:`_LINOPY_DIVERGENCES`); ``tests/test_solve_status.py`` asserts both
#: halves, so linopy moving — or fixing — shows up as a failure here rather
#: than as a status a caller has to know not to trust.
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

#: Where the table above does not copy linopy's, and why. Each is a status
#: whose meaning Gurobi documents and linopy's map contradicts, so copying it
#: would import a wrong answer rather than a shared vocabulary — the same
#: trade :attr:`~lpspec.relational.status.SolveStatus.is_readable` already
#: refused to make once. The vocabulary itself is still linopy's: every value
#: here is one of its termination conditions.
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

    The hand-off without the branch and bound — :func:`solve_gurobi` is this
    plus ``optimize()``, and the seam is the one
    :func:`~lpspec.relational.sinks.highs.build_highs` draws, for the same
    reason: the search is the same work whoever filled the model.

    ``batch_rows`` is the budget in *nonzeros*, spent through
    :mod:`~lpspec.relational.chunking`. The parameter stays so tests can force
    ragged blocks.
    """
    return _load(model, batch_rows, solver_options)[0]


def solve_gurobi(
    model: ModelTables,
    batch_rows: int | None = None,
    solver_options: Mapping[str, Any] | None = None,
) -> tuple[SolveStatus, float, pl.DataFrame | None, pl.DataFrame | None]:
    """Feed the model to Gurobi and solve it.

    Returns ``(status, objective, primal, dual)`` in
    :func:`~lpspec.relational.sinks.highs.solve_direct`'s shape, and answers
    the two ``None`` cases the same way: no primal means the solve left
    nothing worth reading, no dual means the model is mixed-integer or the run
    stopped short of a basis. Gurobi refuses the attribute in both of those
    cases rather than handing back zeros, which is the one place it makes this
    easier than HiGHS does.
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

    The ``MVar`` and the block handles are what carry the solution: ``x.X`` and
    ``block.Pi`` are numpy arrays, where ``getVars()`` / ``getConstrs()`` would
    build one Python object per column and row to read the same numbers.

    The environment is created here and owned by the model — gurobipy holds it
    for as long as the model lives, and releases the licence when the model is
    dropped. ``OutputFlag`` goes in as an environment parameter rather than
    afterwards because that is what suppresses the banner Gurobi prints when a
    default environment is started; ``solver_options`` is applied after and so
    can put the log back.
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
    """The optional dependency, or a message naming the extra that carries it.

    Both halves are named, because the missing one is as often scipy: it is
    the matrix API's currency, not a transitive convenience.
    """
    try:
        import gurobipy
        import scipy.sparse  # noqa: F401 — guarded here so the message covers it
    except ModuleNotFoundError as exc:
        msg = 'The gurobi sink requires the [gurobi] extra (gurobipy, scipy): pip install "lpspec[gurobi]"'
        raise ModuleNotFoundError(msg) from exc
    return gurobipy


def _status_of(m: Any) -> SolveStatus:
    """What the solve concluded, on both axes.

    ``SolCount`` is the solver's own answer to "is there anything here", which
    the termination condition does not give: a run stopped at a limit may or
    may not have found an incumbent.
    """
    code = int(m.Status)
    return SolveStatus(
        termination_condition=_CONDITION_OF_GUROBI_STATUS.get(code, 'unknown'),
        solver_wording=_wording(code),
        has_primal=m.SolCount > 0,
    )


def _wording(code: int) -> str:
    """Gurobi's own name for a status code — ``TIME_LIMIT``, ``SUBOPTIMAL``.

    Read off ``GRB.Status`` rather than tabulated, so a status this package
    has never heard of still reaches the caller as something searchable.
    """
    gurobipy = _gurobipy()
    names = {getattr(gurobipy.GRB.Status, name): name for name in dir(gurobipy.GRB.Status) if not name.startswith('_')}
    return names.get(code, str(code))


def _duals(blocks: list[Any]) -> pl.DataFrame | None:
    """Shadow prices in row order, or ``None`` where the model has none.

    The blocks were added in ascending row ranges and each carries its own
    slice, so concatenating them reproduces the row index without a sort.
    Gurobi refuses ``Pi`` outright on a mixed-integer model — that refusal is
    the answer here, rather than a zero vector to test.
    """
    import numpy as np

    gurobipy = _gurobipy()
    try:
        values = [block.Pi for block in blocks]
    except (AttributeError, gurobipy.GurobiError):
        return None
    return _labelled('row', np.concatenate(values) if values else np.empty(0, dtype=np.float64))


def _labelled(label: str, values: Any) -> pl.DataFrame:
    """``(label, value)`` over the solver's own dense index.

    Solver output is one array per quantity, positionally indexed by the
    solver's index — which *is* our label, densely assigned — so the join
    column is an ``arange`` rather than anything read back.
    """
    import numpy as np

    values = np.asarray(values, dtype=np.float64)
    return pl.DataFrame({label: np.arange(len(values), dtype=np.int64), 'value': values})
