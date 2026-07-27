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
        yield from _chunks(self.row_count, per_chunk)

    def col_chunks(self, per_chunk: int) -> Iterator[tuple[int, int]]:
        """``(lo, hi)`` half-open column ranges covering the model's columns.

        The column twin of :meth:`row_chunks`. Both exist so that a sink can
        bound *every* pass it makes: a query ordered over all columns at once
        is a global sort, and duckdb's sort is the operator that does not
        reliably stay inside ``memory_limit``.
        """
        yield from _chunks(self.column_count, per_chunk)


def _chunks(total: int, per_chunk: int) -> Iterator[tuple[int, int]]:
    """Half-open ``[lo, hi)`` ranges covering ``[0, total)``, in order."""
    for lo in range(0, max(total, 1), per_chunk):
        hi = min(lo + per_chunk, total)
        if hi <= lo:
            return
        yield lo, hi
