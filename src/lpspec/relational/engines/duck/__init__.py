"""The duckdb engine: plan → SQL → `sinks.ModelTables`.

Opt-in — `pip install "lpspec[duckdb]"` — and chosen explicitly with
``lps.build(..., engine='duckdb')``. Never selected for you: there is no
routing here, only a choice, and the two engines answer the same YAML with the
same numbers (`tests/test_engine_parity.py`).

It exists because the build's **peak** is a different question from its speed.
Measured on the write path it holds a model in 2.1-4.2x less memory than the
polars engine at the top of the ladder, and the gap widens with the model; it
costs 1.6-2.4x the wall clock to do it. On the way to a solver, where HiGHS's
own copy dominates, that advantage mostly disappears. `bench/duckdb-spike.md`
is the whole measurement, including the rungs where polars wins.

Choosing it **adds pyarrow**, which the default engine does not need: duckdb
and polars hand frames to each other through Arrow. It does *not* add pandas —
pyarrow imports pandas only when pandas is already installed, which is easy to
mistake for a requirement in a development environment. `tests/test_api.py`
pins both halves.
"""

from lpspec.relational.engines.duck.compiler import DuckCompiler
from lpspec.relational.engines.duck.executor import DuckExecutor

__all__ = ['DuckCompiler', 'DuckExecutor']
