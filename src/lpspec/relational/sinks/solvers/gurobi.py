"""The ``gurobi`` solver: the model in two calls, straight into gurobipy.

Reads the same ``dense_columns`` as
:mod:`~lpspec.relational.sinks.solvers.highs`, so the two cannot disagree
about the model they load. Two things differ:

- **The matrix's currency.** HiGHS takes the three CSR arrays; gurobipy's
  matrix API takes a matrix *object*, so they are wrapped in a
  ``scipy.sparse.csr_matrix`` — a view, not a copy. That wrapper is why the
  ``[gurobi]`` extra carries scipy: the alternative is a Python call per row.
- **Nothing is batched.** The columns cannot be, since ``addMConstr`` writes
  into one ``MVar`` spanning the model — and the matrix *should* not be, which
  is where this sink parts company with the HiGHS one. See
  :func:`_row_blocks`.

``gurobipy`` and ``scipy`` are imported inside the functions, so importing
this module stays free for a caller who never solves with it.
"""

from __future__ import annotations

import weakref
from typing import TYPE_CHECKING, Any

import polars as pl

from lpspec.errors import LpspecError
from lpspec.relational.status import SolveStatus

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from lpspec.relational.sinks.tables import ModelTables


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
    ``batch_rows`` is a *nonzero* budget that splits the matrix across calls;
    it defaults to one call, and see :func:`_row_blocks` for why.

    **The caller owns the model, so the environment follows it.** gurobipy has
    no ``Model.getEnv()``, so a caller handed only the model could never
    release the licence it holds; a finalizer disposes the environment when
    the model is collected, which under refcounting is when the caller drops
    it. That is one thing to own rather than two, and it is why this returns a
    model rather than a pair. (linopy's equivalent — a solver model detached
    from its `Solver` — leaves the environment to gurobipy to free whenever.)
    """
    m, _x, _blocks, environment = _load(model, batch_rows, solver_options)
    weakref.finalize(m, environment.dispose)
    return m


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

    Model and environment are disposed before returning, so the licence is
    released when the solve ends rather than whenever the collector gets to
    it. Everything read back is a numpy array taken before the dispose.
    :func:`build_gurobi` cannot do this — its caller owns the model.
    """
    m, x, blocks, environment = _load(model, batch_rows, solver_options)
    try:
        m.optimize()
        status = _status_of(m)
        if not status.is_readable:
            return status, float('nan'), None, None
        return status, m.ObjVal, _labelled('col', x.X), _duals(model.row_count, blocks)
    finally:
        m.dispose()
        environment.dispose()


def _load(
    model: ModelTables,
    batch_rows: int | None,
    solver_options: Mapping[str, Any] | None,
) -> tuple[Any, Any, list[Any], Any]:
    """The model, the handles to read it back, and the environment to release.

    ``x.X`` and ``block.Pi`` are numpy arrays; ``getVars()`` / ``getConstrs()``
    would build one Python object per column and row to reach the same numbers.
    The environment comes back because gurobipy has no ``Model.getEnv()``, and
    whoever disposes the model has to dispose it too.

    **Options go on the environment, not the model.** A licence parameter —
    ``WLSAccessID``, ``ComputeServer``, ``TokenServer`` — can only be set
    before an environment starts, and ``setParam`` on the model refuses it
    with *unable to modify parameter after environment started*. So a
    Compute-Server or WLS user could not reach this sink at all. Everything
    else is unaffected: an environment's parameters are the defaults of every
    model built on it, and an unknown name still raises at the same point.
    ``OutputFlag`` leads so a caller can put the log back by passing their own.
    """
    gurobipy = _gurobipy()
    import numpy as np
    import scipy.sparse

    environment = gurobipy.Env(params={'OutputFlag': 0, **dict(solver_options or {})})
    m = gurobipy.Model(env=environment)

    lb, ub, cost, integral = model.dense_columns(gurobipy.GRB.INFINITY)
    # an LP pays 17% of the column hand-off for a vtype array of one repeated
    # letter — 0.46 s against 0.38 s at 10^6 columns. linopy skips it the same way.
    discrete: dict[str, Any] = {'vtype': np.where(integral, 'I', 'C')} if integral.any() else {}
    x = m.addMVar(model.column_count, lb=lb, ub=ub, obj=cost, **discrete)

    senses = np.array([gurobipy.GRB.LESS_EQUAL, gurobipy.GRB.GREATER_EQUAL, gurobipy.GRB.EQUAL], dtype='<U1')
    blocks = []
    for rows, a in _row_blocks(model, batch_rows):
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
    return m, x, blocks, environment


def _row_blocks(model: ModelTables, batch_rows: int | None) -> Iterator[tuple[pl.DataFrame, pl.DataFrame]]:
    """The matrix as ``(rows, entries)`` pairs — **one pair unless asked**.

    Where the HiGHS sink batches by default, this one does not, and the
    difference is measured rather than inherited. ``addMConstr`` costs about
    42 ns per *model* column per call, whatever the block holds: at 400k
    columns that is 17 ms of overhead per extra call, so splitting a 2M-nonzero
    matrix into 20 blocks took 0.55 s against 0.23 s in one, and 40 blocks took
    0.89 s. The cost scales with the model's width, so the wider the model the
    worse batching is.

    Batching bought nothing back. It cannot bound the peak here — Gurobi
    accumulates the whole model as it goes, so the only thing a block saves is
    one block-sized temporary: 0.62 GB against 0.65 GB on that model, for twice
    the hand-off time.

    ``batch_rows`` still splits when a caller asks, which is how the tests
    exercise block boundaries — the arithmetic that puts a row's entries in the
    right ``indptr`` slot is worth a test even when nothing runs it by default.
    """
    ordered_rows = model.rows.sort('row').select(
        'row',
        pl.col('sense').replace_strict({'<=': 0, '>=': 1, '==': 2}, return_dtype=pl.UInt8).alias('op'),
        'rhs',
    )
    ordered_matrix = model.matrix.sort('row')
    if batch_rows is None:
        yield ordered_rows, ordered_matrix
        return
    for lo, hi in model.row_chunks_by_nonzeros(batch_rows):
        yield (
            ordered_rows.filter(pl.col('row').is_between(lo, hi, closed='left')),
            ordered_matrix.filter(pl.col('row').is_between(lo, hi, closed='left')),
        )


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


def _duals(row_count: int, blocks: list[Any]) -> pl.DataFrame | None:
    """Shadow prices in row order, or ``None`` where the model has none.

    Blocks were added in ascending row ranges, so concatenating their slices
    reproduces the row index without a sort. Gurobi refuses ``Pi`` on a
    mixed-integer model, and that refusal *is* the answer — no zero vector to
    test.
    """
    import numpy as np

    gurobipy = _gurobipy()
    try:
        slices = [block.Pi for block in blocks]
    except (AttributeError, gurobipy.GurobiError):
        return None
    values = np.concatenate(slices) if slices else np.empty(0, dtype=np.float64)
    if len(values) != row_count:
        raise LpspecError(
            f'the solver returned {len(values)} duals for {row_count} rows. The join that reads '
            f'them back is positional, so a short vector would drop rows silently. This is an '
            f'engine bug rather than a problem with the model — please report it.'
        )
    return _labelled('row', values)


def _labelled(label: str, values: Any) -> pl.DataFrame:
    """``(label, value)`` over the solver's own dense index, which *is* our
    label — so the join column is an ``arange``, not something read back."""
    import numpy as np

    values = np.asarray(values, dtype=np.float64)
    return pl.DataFrame({label: np.arange(len(values), dtype=np.int64), 'value': values})
