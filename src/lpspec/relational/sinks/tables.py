"""What every sink reads, and nothing more.

Four frames plus the scalars a writer needs to size its batching. A sink that
needs a fifth thing states it here, where both sides can see it.

Also the *projections* of them more than one sink needs — the dense column and
row vectors, and the matrix a block at a time. They belong to the contract
rather than to either solver, because two sinks computing them separately could
disagree about the model they loaded — the one thing neither may do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import polars as pl

from lpspec.relational import chunking

if TYPE_CHECKING:
    from collections.abc import Iterator

    import numpy as np
    import numpy.typing as npt

    #: What a solver sink is handed: three float vectors and an integrality
    #: mask, each as long as the model has columns.
    DenseColumns = tuple[
        npt.NDArray[np.float64], npt.NDArray[np.float64], npt.NDArray[np.float64], npt.NDArray[np.bool_]
    ]

    #: A sense code and a right-hand side, each as long as the model has rows.
    DenseRows = tuple[npt.NDArray[np.uint8], npt.NDArray[np.float64]]

    #: One chunk of rows: the half-open label range, the matrix entries those
    #: rows own, and the offset of each row's entries within them.
    RowBlock = tuple[int, int, pl.DataFrame, npt.NDArray[np.int64]]


#: ``sense`` as a number, so a row's comparison crosses into numpy as one byte
#: rather than as a boxed Python string. The order is arbitrary and shared: a
#: solver indexes its own spelling with these, so the two cannot drift.
SENSE_CODES = {'<=': 0, '>=': 1, '==': 2}


@dataclass(frozen=True)
class ModelTables:
    """The built model, as a sink sees it.

    ``cols`` (lb, ub, vtype), ``obj`` (col, coeff), ``rows`` (row, sense,
    rhs) and ``matrix`` in COO (row, col, coeff). The scalars are what a sink
    cannot cheaply recover; the objective constant lives outside the frames
    because it has no column to attach to.

    ``col`` and ``row`` are dense ``0..n-1``, so they *are* the solver's own
    indices and no sink builds a mapping.

    **``cols`` carries no ``col``.** It holds one row per column, in label
    order, so a row's position is its index and the column would be the frame's
    own row number. Every other frame is sparse in the index and keeps it —
    ``obj`` most of all, which looks dense on models where every variable has a
    cost and is not (0.71 of ``cols`` on `transport`).
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
        up at — and ``cols`` already arrives in that order, one row per column,
        so its three vectors are the frame's own and nothing is scattered.
        Only ``cost`` still is, because ``obj`` is genuinely sparse: a variable
        in no objective term has no row, and is left free rather than holding
        whatever the allocator returned.

        **Nothing textual crosses into numpy.** A polars ``String`` converts by
        boxing every value as a Python object, so the test against
        ``'continuous'`` is made in polars and only its answer crosses: 0.04 s
        against 0.95 s at 10M columns.
        """
        import numpy as np

        count = self.column_count
        # `cols` is already the solver's index, so its three vectors need no
        # scatter. `copy=True` rather than in place because they are views of
        # the frame now: rewriting an infinity through one would edit the built
        # model to suit whichever solver asked last.
        lb = np.nan_to_num(self.cols['lb'].to_numpy(), copy=True, neginf=-infinity, posinf=infinity)
        ub = np.nan_to_num(self.cols['ub'].to_numpy(), copy=True, neginf=-infinity, posinf=infinity)
        integral = self.cols.select(pl.col('vtype') != 'continuous').to_series().to_numpy()
        cost = _scattered(count, self.obj['col'].to_numpy(), self.obj['coeff'].to_numpy(), 0.0)
        return lb, ub, cost, integral

    def dense_rows(self, infinity: float) -> DenseRows:
        """``(sense, rhs)`` as numpy vectors over the solver's row index.

        The row half of :meth:`dense_columns`, and for the same reason: ``row``
        is dense ``0..n-1``, so it *is* the position a value belongs at, and a
        chunk of rows is then a slice rather than a search. Sorting the row
        frame and filtering it once per chunk read the same 6M rows nine times
        over on `fleet/l`, to hand each of them over once.

        It stops at the sense rather than at bounds because that is where the
        two solvers part: HiGHS wants a row's ``lower``/``upper`` and Gurobi its
        comparison and right-hand side, and both are this pair spelled
        differently. :data:`SENSE_CODES` is the spelling neither owns.

        A row with no entry is left with a comparison nothing can fail —
        ``>=`` against ``-infinity`` — rather than the ``== 0`` that would be a
        real equality the model never stated.
        """
        sided = self.rows.select(
            'row',
            pl.col('sense').replace_strict(SENSE_CODES, return_dtype=pl.UInt8).alias('op'),
            'rhs',
        )
        at = sided['row'].to_numpy()
        sense = _scattered(self.row_count, at, sided['op'].to_numpy(), SENSE_CODES['>='])
        rhs = _scattered(self.row_count, at, sided['rhs'].to_numpy(), -infinity)
        return sense, rhs

    def row_blocks(self, budget: int | None) -> Iterator[RowBlock]:
        """Each chunk of rows with the matrix entries it owns.

        **``budget=None`` is one block, and that is a real answer rather than a
        degenerate one.** Whether splitting pays is a property of the API being
        fed, not of the model: HiGHS takes a chunk at a time and its budget
        bounds the temporary, while Gurobi's ``addMConstr`` charges about 42 ns
        per *model column* per call whatever the block holds — 0.23 s in one
        call against 0.89 s in forty on the same matrix (#434). So the caller
        says, and both answers come out of the same code.

        The matrix is ordered once, and a chunk is then a ``slice`` of it
        located by binary search on the label column — the range is contiguous
        because ``row`` is dense and the frame is sorted, so scanning for it
        would re-read the whole model once per chunk.

        **Searched in polars rather than through numpy.** Pulling the label
        column out to search it there is marginally faster and holds a second
        copy of one column of the model for the whole loop, which is 0.11 GB at
        `transport/l` — the wrong trade in a pass that exists to stay bounded.

        ``starts`` is each row's offset within the block, which is what both
        solvers' matrix APIs ask for. A row with no entries takes the next
        row's offset, and so occupies no span.
        """
        import numpy as np

        ordered = self.matrix.sort('row')
        label = ordered['row']
        spans = [(0, self.row_count)] if budget is None else self.row_chunks_by_nonzeros(budget)
        for lo, hi in spans:
            first = int(label.search_sorted(lo, 'left'))
            last = int(label.search_sorted(hi, 'left'))
            entries = ordered.slice(first, last - first)
            yield lo, hi, entries, np.searchsorted(entries['row'].to_numpy(), np.arange(lo, hi))


def _scattered(count: int, at: Any, values: Any, absent: Any) -> Any:
    """*values* written at the label each one belongs to, *absent* elsewhere."""
    import numpy as np

    dense = np.full(count, absent, dtype=values.dtype)
    dense[at] = values
    return dense
