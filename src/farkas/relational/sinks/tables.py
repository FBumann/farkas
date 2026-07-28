"""What every sink reads, and nothing more.

The contract between the executor and the sinks: four tables in a connection,
plus the handful of scalars a writer needs to size its own chunking. A sink
that needs a fifth thing states it here, where both sides can see it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from farkas.relational import chunking

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

    def row_chunks_by_nonzeros(self, budget: int) -> Iterator[tuple[int, int]]:
        """Row ranges holding roughly ``budget`` *nonzeros* each.

        A sink that reads ``A`` a range at a time pays in nonzeros, not in
        rows — a range of 100k rows is 900k entries in one model and 10M in
        another, and only the second is a problem. So the width here is the
        average row, and there is deliberately no row-counted twin to reach
        for by mistake.
        """
        nonzeros = self.scalar('SELECT count(*) FROM A')
        return chunking.ranges(self.row_count, budget, nonzeros / max(1, self.row_count))

    def col_chunks(self, budget: int) -> Iterator[tuple[int, int]]:
        """Column ranges of roughly ``budget`` columns each.

        Width 1, because a column *is* one row of the batch a sink hands over —
        stated rather than assumed, which is the bargain
        :mod:`~farkas.relational.chunking` asks for. Ranges exist at all
        because a query ordered over every column at once is a global sort,
        and duckdb's sort is the operator that does not reliably stay inside
        ``memory_limit``.
        """
        return chunking.ranges(self.column_count, budget, 1.0)
