"""A duckdb engine behind the same plan, for pricing the SQL.

Not a shipped lane and not reachable from `lpspec.api` — `bench/duckdb-spike.md`
costed a return to duckdb by counting lines, and a count cannot say whether the
result reads. This is the answer to that, written to the same seam so the two
can be diffed rather than argued about: `DuckExecutor.build()` produces the
`sinks.ModelTables` the polars executor produces, and every sink below it is
shared and unaware.

`tests/test_duck_parity.py` is what makes it evidence: it builds the same
programs both ways and compares the four frames exactly.
"""

from lpspec.relational.duck.compiler import DuckCompiler
from lpspec.relational.duck.executor import DuckExecutor

__all__ = ['DuckCompiler', 'DuckExecutor']
