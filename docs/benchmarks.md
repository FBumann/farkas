# Measured results: streaming vs eager LP construction

Evidence for the central claim — build peak RSS is a config knob
(`memory_limit`), not O(dense dim product) — and for where that claim currently
stops holding.

Everything under [Results](#results) is produced by [`bench/`](../bench/README.md)
and read straight off [`bench/results/latest.jsonl`](../bench/results/latest.jsonl),
which carries the machine fingerprint and library versions that produced it:

```bash
uv run python -m bench.run --sizes xs s m l --repeat 2 --memory-limits 256MB 1GB 4GB
uv run python -m bench.report bench/results/latest.jsonl
```

Both arms build the *same* YAML through the two lanes and write an LP file —
farkas via `fk.build` + `write_lp`, linopy via `Model.to_file(io_api='lp-polars')`
— so the only difference is the engine. Before anything is timed, the smallest
rung of each case is solved on both arms and the objectives compared; the run
aborts on a mismatch, because a perf number describing two different models is
worse than no number. Peak RSS is `ru_maxrss`, the counter `/usr/bin/time -l`
reports, one process per measurement.

## Results

macOS 26.2, M-series, 24 GB RAM · python 3.13.2 · farkas 0.0.0a13, linopy 0.9.0,
duckdb 1.5.5, polars 1.43.0, xarray 2026.7.0. Parity gate: both cases agree to
0 relative (bit-identical objectives). Best of two repeats; wall time excludes
import.

### dispatch — pointwise bounds + one `sum` per row

| variables | rows | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|
| 10k | 100 | 0.05 s | 0.18 s | 0.26x | 0.16 GB | 0.21 GB | 0.74x | 1 MB |
| 100k | 1k | 0.19 s | 0.22 s | 0.85x | 0.18 GB | 0.26 GB | 0.69x | 7 MB |
| 1M | 10k | 0.82 s | 0.40 s | 2.04x | 0.40 GB | 0.59 GB | 0.67x | 74 MB |
| 10M | 100k | 6.67 s | 3.19 s | 2.09x | 0.88 GB | 2.19 GB | 0.40x | 768 MB |

### transport — three `group_sum` joins per row

| variables | rows | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|
| 9.8k | 1.4k | 0.08 s | 0.34 s | 0.24x | 0.17 GB | 0.22 GB | 0.76x | 1 MB |
| 98k | 14k | 0.24 s | 0.36 s | 0.66x | 0.20 GB | 0.28 GB | 0.71x | 7 MB |
| 980k | 140k | 1.41 s | 0.56 s | 2.53x | 0.40 GB | 0.65 GB | 0.63x | 76 MB |
| 9.8M | 1.4M | 9.25 s | 3.96 s | 2.34x | 1.35 GB | 1.88 GB | 0.72x | 794 MB |

### Peak RSS against the budget

farkas at three `memory_limit` settings, 16× apart end to end:

| case | variables | `256MB` | `1GB` | `4GB` | linopy |
|---|---|---|---|---|---|
| dispatch | 10k | 0.16 GB | 0.16 GB | 0.16 GB | 0.21 GB |
| dispatch | 100k | 0.18 GB | 0.18 GB | 0.18 GB | 0.26 GB |
| dispatch | 1M | 0.39 GB | 0.40 GB | 0.41 GB | 0.59 GB |
| dispatch | 10M | 0.77 GB | 0.88 GB | 0.90 GB | 2.19 GB |
| transport | 9.8k | 0.17 GB | 0.17 GB | 0.16 GB | 0.22 GB |
| transport | 98k | 0.20 GB | 0.20 GB | 0.20 GB | 0.28 GB |
| transport | 980k | 0.39 GB | 0.40 GB | 0.42 GB | 0.65 GB |
| transport | 9.8M | **OOM** | 1.35 GB | 1.34 GB | 1.88 GB |

## What the numbers say

**The memory win is real but modest at these sizes, and it widens with the
model.** 0.74× at 10k is nothing but a fixed import floor (~0.16 GB of python +
duckdb + pyarrow, which the eager lane pays too, plus xarray). At 10M variables
dispatch is 2.5× better; the earlier spike measured 13.6× at 35.6M, and the trend
here is consistent with that even though the absolute numbers are not comparable.

**farkas is ~2× slower to build once the model is over ~10⁶ variables, and
faster below ~10⁵** — the crossover is linopy's fixed overhead, not anything
about the engines. This matches the earlier finding and is the honest headline:
the trade is memory for wall time.

**Peak does not track the budget.** Across a 16× range of `memory_limit`, peak
RSS at 10M variables moves 0.77 → 0.90 GB — under 20%, and 2–3× the budget
itself. Whatever dominates at this scale is outside duckdb's accounting (LP text
buffers, Arrow batches, and the label tables are the candidates), so hard rule 4
is currently supported in its *comparative* form (peak is flat in model size
relative to the eager lane) and **not** in its literal one (peak is a function of
the budget). Wall time is likewise budget-insensitive, which the earlier harness
also found.

**One shape OOMs instead of spilling.** `transport` at 9.8M variables under
256 MB dies in the terminal `SUM(coeff) GROUP BY row, col` assembly
(`executor.py::_build_constraint`) — the one place the code comments assert that
"joins and the plain numeric hash aggregate spill under `memory_limit` on their
own — no chunking needed". At the same size `dispatch` is fine, so the trigger is
the `group_sum` join fan-in, not scale alone. Operational finding 1 said not every
operator spills; this is the same class, now in a shape the shipped executor does
not chunk.

## Earlier numbers, not reproducible

These came from a harness under `scratch/` that has since been removed. They are
kept because the 107M-variable run is a capability proof nothing above replaces —
a model whose dense build does not fit on the machine at all — and because the
runtime attribution still holds. They cannot be re-run: different model sizes,
different code.

![peak RSS, streaming vs eager](bench.svg)

| model | backend | budget | peak RSS | wall | LP size |
|---|---|---|---|---|---|
| S=100k (8.9M vars) | linopy lp-polars | — | 2.26 GB | 1.6–1.9 s | 555 MB |
| S=100k | duckdb | 1 GB / chunk 25k | 0.81 GB | 3.1 s | 637 MB |
| S=400k (35.6M vars) | linopy lp-polars | — | 6.60 GB | 7.8 s | 2.31 GB |
| S=400k | duckdb | 512 MB / chunk 25k | **0.49 GB** | 14.5 s | 2.64 GB |
| S=1.2M (107M vars) | linopy lp-polars | — | ~20 GB extrapolated — exceeds this machine | — | — |
| S=1.2M | duckdb | 512 MB / chunk 25k | 0.57 GB | 74 s | 7.99 GB |

Runtime there was dominated by fixed work — re-scanning the vars table per
section, ~100M `printf` calls, a 2.6 GB CSV write — not by spilling, which is why
a bigger budget did not help. End-to-end `solver_direct` (batched HiGHS
`addCols`/`addRows`, no LP file) at 35.6M variables: solve 30.5 s, Optimal,
objective identical to the oracle; process peak 5.76 GB dominated by HiGHS's own
model and simplex workspace, the residency hard rule 4 exempts.

## Not measured yet

In rough order of what would change a decision: `solver_direct` end-to-end (the
shipped path, where the LP file is not written at all); `storage` (`roll`, the
bounded-halo self-join); a MILP, where solve time dwarfs build; a `where`-density
sweep, since masks are row absence here and NaN-dense in xarray — the one axis
where the gap should be qualitative rather than 2×; and a hand-written
highspy/CSR arm as the speed-of-light floor, without which "2× slower than
linopy" has no denominator.

## Operational findings

Load-bearing for `relational/executor.py`; its module docstring states the
resulting rules and this is the evidence behind them.

1. **Not every relational operator spills.** duckdb raises OOM rather than
   exceed `memory_limit` when the plan needs an unspillable operator:
   `string_agg(... ORDER BY ...)` buffers everything (plain `string_agg` is
   fine at moderate group counts); a global-`ORDER BY` window (`ROW_NUMBER` for
   labels) materialises its whole input and OOMs at ~35M rows under tight
   budgets; even a plain external-sort rewrite of the constraint section OOMs
   below ~1 GB at 9.3M rows.
2. **Partition-wise execution is the fix.** Chunking by the leading foreach dim
   bounds every operator: labels = per-chunk `ROW_NUMBER` + running offset;
   constraint blocks = per-chunk `GROUP BY` + `COPY`, parts concatenated. Chunk
   size and memory limit trade off directly; 25k snapshots fits in 256 MB.
3. **LP section order is free.** Line order inside sections is irrelevant to
   solvers (labels live in the text), so no global sorts are needed — hence
   `SET preserve_insertion_order=false`.
4. **Measurement pitfalls.** memray's tracker slows duckdb ~8× (allocation
   interception on a multithreaded, allocation-heavy engine) while barely
   affecting linopy — never benchmark *runtime* under memray, and its allocator
   peak overcounts polars (reserved arenas). Use untracked `/usr/bin/time -l`
   peak RSS as the gate metric; memray for attribution only.
5. **A file-backed duckdb database** (not `:memory:`) is required for the buffer
   pool to spill table data under `memory_limit`.
6. **Constraint assembly is a counter-example to finding 1's exception list.**
   `_build_constraint` runs one unchunked
   `INSERT INTO A … SELECT row, col, SUM(coeff) … GROUP BY row, col`, on the
   stated grounds that joins and plain numeric hash aggregates spill on their
   own. `transport` at 9.8M variables under a 256 MB budget raises
   `OutOfMemoryException` there; `dispatch` at 10M does not. The difference is
   the `group_sum` join fan-in — three joined term streams unioned into one
   aggregate — so the rule wants narrowing to "spills, unless the fan-in is
   wide", and the fix is the same partition-wise treatment finding 2 describes.

## Sink capabilities

What each sink can ingest, measured against the shipped solvers rather than
assumed. The architectural reading is in
[ARCHITECTURE.md](../ARCHITECTURE.md#capability-is-not-the-ceiling); the plan is
[ROADMAP Track 4](../ROADMAP.md#track-4--sink-capabilities).

| | `lp_file` | HiGHS direct | Gurobi direct |
|---|---|---|---|
| affine rows, COO, integrality | text | native | native |
| semi-continuous | text | `kSemiContinuous` | native |
| SOS1 / SOS2 | text section | **no concept** — `HighsLp` has no SOS field, no `addSos` | `addSOS` |
| indicator | text section | **no concept** | `addGenConstrIndicator` |
| quadratic objective | text section | `passHessian` — but `Hessian + integrality` returns `kError`, so no MIQP | native, incl. MIQP |

HiGHS results are measured here; Gurobi's are from the API and linopy's
`SolverFeature` table, and want a spike before they are relied on. linopy
declares HiGHS with `INTEGER_VARIABLES` *and* `QUADRATIC_OBJECTIVE` in one flat
`frozenset`, so its own model reports MIQP as available — the conjunction is
what a capability descriptor has to express.

### The quadratic handoff

Neither direct API has an incremental counterpart to batched
`addCols`/`addRows`: `passHessian` and `setMObjective` take the quadratic part
whole. Under the aligned-only scope (`variable × variable` at the same
coordinates) `Q` is **diagonal**, so it costs 16 bytes per quadratic column:

| quadratic cols | diagonal Hessian |
|---|---|
| 10⁷ | 0.16 GB |
| 3.56×10⁷ | 0.57 GB |
| 10⁸ | 1.60 GB |

Against the measured `solver_direct` peak of 5.76 GB at 35.6M variables —
already dominated by HiGHS's own model, which hard rule 4 exempts — 0.57 GB is
~10%. On `lp_file`, where nothing is exempt, a quadratic objective is a text
section and streams like any other. So this is a cost, not an invariant
violation. Two caveats:

- HiGHS accepts `dim_ < num_col` (verified), so ordering quadratic variables
  first bounds the Hessian to that block rather than the whole model.
- **The diagonal argument dies with the aligned restriction.** General bilinear
  `Q` is not diagonal, and then peak stops tracking the budget — a second,
  independent reason that restriction is load-bearing.
