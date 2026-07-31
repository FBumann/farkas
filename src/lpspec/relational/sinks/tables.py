"""What every sink reads, and nothing more.

Four frames plus the scalars a writer needs to size its batching. A sink that
needs a fifth thing states it here, where both sides can see it.

Also the one *projection* of them more than one sink needs
(:meth:`ModelTables.dense_columns`). It belongs to the contract rather than to
either solver, because two sinks computing it separately could disagree about
the model they loaded — the one thing neither may do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, get_args

import polars as pl

from lpspec.relational import chunking, plan

if TYPE_CHECKING:
    from collections.abc import Iterator

    import numpy as np
    import numpy.typing as npt

    #: What a solver sink is handed: three float vectors and an integrality
    #: mask, each as long as the model has columns.
    DenseColumns = tuple[
        npt.NDArray[np.float64], npt.NDArray[np.float64], npt.NDArray[np.float64], npt.NDArray[np.bool_]
    ]


#: The columns of each frame, in order.
COLS = ('col', 'lb', 'ub', 'vtype')
OBJ = ('col', 'coeff')
ROWS = ('row', 'sense', 'rhs')
MATRIX = ('row', 'col', 'coeff')

#: And the dtype of each column. Here rather than in an executor because it is
#: what a *sink* reads: two engines filling the same four frames with different
#: types is a difference no sink asked for and none can see coming.
#:
#: ``vtype`` is an ``Enum`` over the variable types the plan declares, rather
#: than a string: it holds one word per column and the same handful of words
#: for the whole model, so as a string it stores that word once per row —
#: 0.098 GB of the ``cols`` frame's 0.333 at 9.8M columns, against 0.010 as an
#: Enum. The Enum also makes the vocabulary explicit, so a fourth variable type
#: added to :data:`~lpspec.relational.plan.VariableType` and not reaching here
#: fails where the column is built rather than in whichever sink first compares
#: against a name it does not know.
DTYPES = {
    'col': pl.Int64, 'row': pl.Int64,
    'lb': pl.Float64, 'ub': pl.Float64, 'rhs': pl.Float64, 'coeff': pl.Float64,
    'sense': pl.String, 'vtype': pl.Enum(get_args(plan.VariableType)),
}  # fmt: skip

#: The variable-type column's dtype, which an engine builds a literal against.
VTYPE = DTYPES['vtype']


@dataclass(frozen=True)
class ModelTables:
    """The built model, as a sink sees it.

    ``cols`` (col, lb, ub, vtype), ``obj`` (col, coeff), ``rows`` (row, sense,
    rhs) and ``matrix`` in COO (row, col, coeff). The scalars are what a sink
    cannot cheaply recover; the objective constant lives outside the frames
    because it has no column to attach to.

    ``col`` and ``row`` are dense ``0..n-1``, so they *are* the solver's own
    indices and no sink builds a mapping.
    """

    cols: pl.DataFrame
    obj: pl.DataFrame
    rows: pl.DataFrame
    matrix: pl.DataFrame
    column_count: int
    row_count: int
    objective_sense: str
    objective_constant: float

    def row_chunks_by_nonzeros(self, budget: int) -> Iterator[tuple[int, int]]:
        """Row ranges holding roughly ``budget`` *nonzeros* each.

        A sink that reads ``matrix`` a range at a time pays in nonzeros, not in
        rows — a range of 100k rows is 900k entries in one model and 10M in
        another, and only the second is a problem. So the width here is the
        average row, and there is deliberately no row-counted twin to reach
        for by mistake.
        """
        return chunking.ranges(self.row_count, budget, self.matrix.height / max(1, self.row_count))

    def col_chunks(self, budget: int) -> Iterator[tuple[int, int]]:
        """Column ranges of roughly ``budget`` columns each.

        Width 1, because a column *is* one row of the batch a sink hands over —
        stated rather than assumed, which is the bargain
        :mod:`~lpspec.relational.chunking` asks for.
        """
        return chunking.ranges(self.column_count, budget, 1.0)

    def dense_columns(self, infinity: float) -> DenseColumns:
        """``(lb, ub, cost, integral)`` as numpy vectors over the solver's index.

        *infinity* is the solver's own spelling of an absent bound — the one
        thing the two disagree on — so it is asked for rather than assumed and
        the vectors come back ready to hand over unedited.

        ``col`` is dense ``0..n-1``, so it *is* the position a value has to end
        up at: lining a frame up with the solver's index is a scatter, and
        neither the join that fills the objective's gaps nor the sort that
        orders the bounds does anything this does not. That pair had to be
        collected whole before the first batch could go over, which cost more
        than the model does. A column with no row is left free rather than
        holding whatever the allocator returned.

        **Nothing textual crosses into numpy.** A polars ``String`` converts by
        boxing every value as a Python object, so the test against
        ``'continuous'`` is made in polars and only its answer crosses: 0.04 s
        against 0.95 s at 10M columns.
        """
        import numpy as np

        count = self.column_count
        at = self.cols['col'].to_numpy()
        lb = _scattered(count, at, self.cols['lb'].to_numpy(), -infinity)
        ub = _scattered(count, at, self.cols['ub'].to_numpy(), infinity)
        integral = _scattered(
            count, at, self.cols.select(pl.col('vtype') != 'continuous').to_series().to_numpy(), False
        )
        cost = _scattered(count, self.obj['col'].to_numpy(), self.obj['coeff'].to_numpy(), 0.0)
        np.nan_to_num(lb, copy=False, neginf=-infinity, posinf=infinity)
        np.nan_to_num(ub, copy=False, neginf=-infinity, posinf=infinity)
        return lb, ub, cost, integral


def _scattered(count: int, at: Any, values: Any, absent: Any) -> Any:
    """*values* written at the column each one belongs to, *absent* elsewhere."""
    import numpy as np

    dense = np.full(count, absent, dtype=values.dtype)
    dense[at] = values
    return dense
