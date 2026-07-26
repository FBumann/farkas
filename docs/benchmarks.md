# Measured results: streaming vs eager LP construction

Evidence for the central claim — build peak RSS is a config knob
(`memory_limit`), not O(dense dim product).

![peak RSS, streaming vs eager](bench.svg)

These numbers came from a harness under `scratch/` that has since been removed:
it had to be re-run by hand, drifted from the shipped code, and its correctness
arm is now the differential test suite, which runs on every commit. Reproducing
them means writing a fresh harness against `farkas.api`; the pitfalls
below are what to watch for.

## Results

macOS, M-series, 24 GB RAM, linopy 0.9.0 / duckdb 1.5.5. Peak RSS via
`/usr/bin/time -l`, untracked. Dispatch model (`examples/dispatch.yaml`),
G=100 (89 active after mask).

| model | backend | budget | peak RSS | wall | LP size |
|---|---|---|---|---|---|
| S=100k (8.9M vars) | linopy lp-polars | — | 2.26 GB | 1.6–1.9 s | 555 MB |
| S=100k | duckdb | 1 GB / chunk 25k | 0.81 GB | 3.1 s | 637 MB |
| S=100k | duckdb | 256 MB / chunk 25k | 0.73 GB | 3.2 s | 637 MB |
| S=400k (35.6M vars) | linopy lp-polars | — | 6.60 GB | 7.8 s | 2.31 GB |
| S=400k | duckdb | 512 MB / chunk 25k | **0.49 GB** | 14.5 s | 2.64 GB |
| S=400k | duckdb | 2 GB / chunk 100k | 1.19 GB | 16.3 s | 2.64 GB |
| S=1.2M (107M vars) | linopy lp-polars | — | ~20 GB extrapolated — exceeds this machine | — | — |
| S=1.2M | duckdb | 512 MB / chunk 25k | 0.57 GB | 74 s | 7.99 GB |

**Verdict:** the peak-RSS win is decisive (13.6× at 35.6M vars) and flat in
model size. The 107M-var run is the capability proof: a model whose dense build
cannot fit on this machine streams out at 0.57 GB. Runtime is ~2× slower than
linopy's lp-polars writer and does *not* improve with a bigger budget — it is
dominated by fixed work (re-scanning the vars table per section, ~100M `printf`
calls, a 2.6 GB CSV write), not by spilling.

Differential check (HiGHS solve, objective + dimensions): equivalent at 1.6k,
920k and 8.9M variables; at 8.9M the objectives agree to 14 significant digits.

The general plan + `DuckdbExecutor` path matches the hand-written spike SQL: at
S=100k under 512 MB, build 3.1 s, `write_lp` 2.0 s, 0.84 GB peak — no
generality penalty against the hand-written 3.1 s / 0.81 GB. End-to-end
`solver_direct` (batched HiGHS `addCols`/`addRows`, no LP file): solve 30.5 s,
Optimal, objective identical to the oracle; process peak 5.76 GB is dominated
by HiGHS's own model and simplex workspace — the irreducible floor — while the
build side stays at the configured budget.

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
