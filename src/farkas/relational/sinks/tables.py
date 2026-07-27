"""What every sink reads, and nothing more.

The contract between the executor and the sinks: four frames, plus the handful
of scalars a writer needs to size its own batching. A sink that needs a fifth
thing states it here, where both sides can see it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    import polars as pl


@dataclass(frozen=True)
class ModelTables:
    """The built model, as a sink sees it.

    Four frames — ``cols`` (col, lb, ub, vtype), ``obj`` (col, coeff), ``rows``
    (row, sense, rhs) and ``matrix``, the coefficient matrix in COO form
    (row, col, coeff). The scalars alongside are the ones a sink cannot cheaply
    recover: the counts it batches by, and the objective's sense and constant,
    which live outside the frames because a constant has no column to attach
    to.

    ``col`` and ``row`` are dense ``0..n-1`` by construction, so they *are* the
    solver's own indices and no sink has to build a mapping.
    """

    cols: pl.DataFrame
    obj: pl.DataFrame
    rows: pl.DataFrame
    matrix: pl.DataFrame
    column_count: int
    row_count: int
    objective_sense: str
    objective_constant: float

    def row_chunks(self, per_chunk: int) -> Iterator[tuple[int, int]]:
        """``(lo, hi)`` half-open row ranges covering the constraint matrix."""
        for lo in range(0, max(self.row_count, 1), per_chunk):
            hi = min(lo + per_chunk, self.row_count)
            if hi <= lo:
                return
            yield lo, hi
