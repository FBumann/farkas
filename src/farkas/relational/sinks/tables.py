"""What every sink reads, and nothing more.

The contract between the executor and the sinks: four tables in a connection,
plus the handful of scalars a writer needs to size its own chunking. A sink
that needs a fifth thing states it here, where both sides can see it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@dataclass(frozen=True)
class ModelTables:
    """The built model, as a sink sees it.

    ``connection`` holds four tables — ``cols`` (col, lb, ub, vtype), ``obj``
    (col, coeff), ``rows`` (row, sense, rhs) and ``A`` (row, col, coeff). The
    scalars alongside are the ones a sink cannot cheaply recover: the counts
    it chunks by, and the objective's sense and constant, which live outside
    the tables because a constant has no column to attach to.
    """

    connection: Any
    workdir: Path
    chunk_rows: int
    column_count: int
    row_count: int
    objective_sense: str
    objective_constant: float

    def scalar(self, sql: str) -> Any:
        row = self.connection.execute(sql).fetchone()
        assert row is not None
        return row[0]

    def row_chunks(self, per_chunk: int) -> Iterator[tuple[int, int]]:
        """``(lo, hi)`` half-open row ranges covering the constraint matrix."""
        for lo in range(0, max(self.row_count, 1), per_chunk):
            hi = min(lo + per_chunk, self.row_count)
            if hi <= lo:
                return
            yield lo, hi
