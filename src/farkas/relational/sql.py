"""How a value is spelled inside SQL text.

Almost nothing in this engine needs quoting, because almost nothing reaches
SQL unchecked: declared names pass ``_IDENT`` before any statement is built,
and the where-grammar admits no string literal at all. Filesystem paths are
the exception — they come from the calling program, not the model, and no
rule constrains them.

So the rule lives here once rather than at each of the eight call sites that
used to interpolate a path directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def path_literal(path: str | Path) -> str:
    """A filesystem path as a SQL string literal, quotes included.

    ``o'brien/load.parquet`` is a legal path and used to end the statement
    early. Doubling is SQL's own escape, and the one duckdb accepts in the
    ``COPY … TO`` position where a bound parameter is not allowed.
    """
    return "'" + str(path).replace("'", "''") + "'"
