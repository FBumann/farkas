"""What every sink reads, and nothing more.

Four frames plus the scalars a writer needs to size its batching. A sink that
needs a fifth thing states it here, where both sides can see it.

Also the one *projection* of those frames more than one sink needs — the
columns laid out on the solver's own index (:meth:`ModelTables.dense_columns`).
It belongs to the contract rather than to either solver sink, because two
sinks computing it separately could disagree about the model they loaded, and
that is precisely the thing neither may do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import polars as pl

from lpspec.relational import chunking

if TYPE_CHECKING:
    from collections.abc import Iterator


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

    def dense_columns(self, infinity: float) -> tuple[Any, Any, Any, Any]:
        """``(lb, ub, cost, integral)`` as numpy vectors over the solver's index.

        Here rather than in a sink because both solver sinks need exactly
        this, and "the two build the same model integer for integer" is a
        claim better held by construction than by two copies staying in step.
        *infinity* is the one thing they disagree on — HiGHS and Gurobi spell
        an absent bound as different numbers — so it is asked for rather than
        assumed, and the vectors come back ready to hand over unedited.

        ``col`` is dense ``0..n-1``, so it *is* the position a value has to end
        up at: lining a frame up with the solver's index is a scatter, and
        neither the join that fills the objective's gaps nor the sort that puts
        the bounds in order has anything to do that this does not. The frame
        those two produced had to be collected whole before the first batch
        could be handed over, which cost more than the model does.

        A column the tables somehow have no row for is left free rather than
        left holding whatever the allocator returned.

        **Nothing textual crosses into numpy.** A polars ``String`` column
        converts by boxing every value as a Python object, so a comparison
        against ``'continuous'`` is made in polars and only its answer — a
        bool — is handed over. At 10M columns the same test costs 0.95 s
        across the boundary and 0.04 s on this side of it.
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
