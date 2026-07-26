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

if TYPE_CHECKING:
    from farkas.relational.sinks.tables import ModelTables


def solve_direct(model: ModelTables, batch_rows: int = 100_000) -> tuple[str, float]:
    """Stream the model into HiGHS and solve it. Returns ``(status, objective)``.

    Leaves the primal values in a ``sol`` table on the connection, so reading
    results back stays a label join like every other read — the caller owns
    the mapping from solver column index to coordinates.
    """
    import highspy
    import numpy as np

    con = model.connection
    inf = highspy.kHighsInf
    h = highspy.Highs()
    h.setOptionValue('output_flag', False)

    empty_i = np.empty(0, dtype=np.int32)
    empty_f = np.empty(0, dtype=np.float64)
    # Arrow -> numpy per column, never through python objects. ``to_pydict()``
    # builds a python float per cell, which ``np.asarray`` then parses back:
    # five million throwaway objects per million columns, and 26x the cost of
    # reading the buffer that is already there.
    reader = con.execute(
        'SELECT c.lb, c.ub, COALESCE(o.coeff, 0) AS cost FROM cols c LEFT JOIN obj o USING (col) ORDER BY c.col'
    ).to_arrow_reader(batch_rows)
    for batch in reader:
        lb = np.nan_to_num(_column(batch, 'lb', np.float64), neginf=-inf, posinf=inf)
        ub = np.nan_to_num(_column(batch, 'ub', np.float64), neginf=-inf, posinf=inf)
        cost = _column(batch, 'cost', np.float64)
        h.addCols(len(cost), cost, lb, ub, 0, empty_i, empty_i, empty_f)

    # Integrality is its own streamed pass rather than a per-batch string
    # compare: for a pure LP the query returns nothing and the loop never runs.
    integral = con.execute("SELECT col FROM cols WHERE vtype != 'continuous' ORDER BY col").to_arrow_reader(batch_rows)
    for batch in integral:
        index = _column(batch, 'col', np.int32)
        h.changeColsIntegrality(
            len(index), index, np.full(len(index), int(highspy.HighsVarType.kInteger), dtype=np.uint8)
        )

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

    status = str(h.getModelStatus()).rsplit('.', 1)[-1].removeprefix('k')
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


def _column(batch: Any, name: str, dtype: Any = None):
    """One Arrow column as a numpy array, without going through python objects."""
    array = batch.column(name).to_numpy(zero_copy_only=False)
    return array if dtype is None else array.astype(dtype, copy=False)
