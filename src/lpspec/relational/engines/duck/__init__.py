"""The duckdb engine: plan → SQL → `sinks.ModelTables`.

Opt-in — `pip install "lpspec[duckdb]"` — and chosen with `LPSPEC_ENGINE`.
Never selected for you: there is no routing here, only a choice, and the two
engines answer the same YAML with the same numbers
(`tests/test_engine_parity.py`).

It exists because the build's **peak** is a different question from its speed,
and it earns its keep at the top of the ladder. On `dispatch/xl` — 40M columns
to an LP file — it builds in 2.66 s against the polars engine's 6.84 s, at the
same peak (4.90 GB against 4.80). At the `l` rung the build phase is 0.55x on
`dispatch`, 1.06x on `nodal` and 1.39x on the join-heavy `transport`, and peak
is 0.88-0.92x except on `transport`'s write path, where it is 1.45x.

**Below `l` it loses**, 1.6-2.5x slower: what is left there is per-statement
overhead, which is fixed and so dominates a small model.

`bench/duckdb-spike.md` is the whole measurement. Read §7 as the provenance of
the *decision* rather than as the current cost — it records the out-of-tree
engine this one was ported from, whose 2.1-4.2x memory advantage the port has
never reproduced.

Choosing it **adds pyarrow**, which the default engine does not need: duckdb
and polars hand frames to each other through Arrow. It does *not* add pandas —
pyarrow imports pandas only when pandas is already installed, which is easy to
mistake for a requirement in a development environment. `tests/test_api.py`
pins both halves.
"""

from lpspec.relational.engines.duck.compiler import DuckCompiler
from lpspec.relational.engines.duck.executor import DuckExecutor

__all__ = ['DuckCompiler', 'DuckExecutor']
