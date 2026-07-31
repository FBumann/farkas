"""The duckdb engine: plan → SQL → `sinks.ModelTables`.

Opt-in — `pip install "lpspec[duckdb]"` — and chosen with `LPSPEC_ENGINE`.
Never selected for you: there is no routing here, only a choice, and the two
engines answer the same YAML with the same numbers
(`tests/test_engine_parity.py`).

**On the committed ladder it does not currently win anything.** Measured
against the default engine on all six bench cases, both sinks, the `m` and `l`
rungs: the build phase is **1.5-4.0x slower**, never faster, and the ratio
holds at the top rung too (`dispatch/xl`, 40M columns: 1.57 s against 0.83 s)
and across a mask-density sweep (2.3-3.4x at every density). Peak is a wash
rather than a win — lighter on `dispatch`, `fleet` and `nodal` (0.83-0.97x),
heavier on `profiled`, `sector` and `transport` (1.02-1.46x).

That is a change, and a recent one. It was ahead on the build phase at the top
of the ladder until the polars engine took five optimisations of its own
(#408, #412, #413, #414, #415), which cut its `dispatch/xl` build from 6.84 s
to 0.83 s. Nothing about this engine regressed; the other one moved.

What the ladder cannot say is what happens above it. Every rung here fits in
RAM, so the argument this engine was originally built on — a model that does
not — is untested rather than refuted.

`bench/duckdb-spike.md` is the whole measurement, including §7, which records
the *out-of-tree* engine this one was ported from and whose 2.1-4.2x memory
advantage the port never reproduced.

Choosing it **adds pyarrow**, which the default engine does not need: duckdb
and polars hand frames to each other through Arrow. It does *not* add pandas —
pyarrow imports pandas only when pandas is already installed, which is easy to
mistake for a requirement in a development environment. `tests/test_api.py`
pins both halves.
"""

from lpspec.relational.engines.duck.compiler import DuckCompiler
from lpspec.relational.engines.duck.executor import DuckExecutor

__all__ = ['DuckCompiler', 'DuckExecutor']
