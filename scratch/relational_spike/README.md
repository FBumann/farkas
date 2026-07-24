# Phase-1 spike: relational/streaming LP construction

Hand-translation of `examples/dispatch.yaml` into duckdb SQL over parquet,
streaming the LP file via `COPY`, benchmarked against linopy's eager build +
`lp-polars` writer. See the project handoff: the goal is to show that build
peak RSS becomes a config knob (`memory_limit`) instead of O(dense dim product).

**Gate:** meaningful peak-RSS win at equal-or-better runtime.

## Files

- `gen_data.py` — writes `generators.parquet` (generator, p_max, cost) and
  `load.parquet` (snapshot, load); `--masked-frac` zeroes a fraction of `p_max`
  to exercise the mask.
- `linopy_baseline.py` — eager oracle: dense xarray build with `mask=`,
  LP via `m.to_file(io_api="lp-polars")`.
- `duckdb_spike.py` — the relational mapping under test:
  mask → row absence (`WHERE p_max > 0`), broadcast → join,
  `sum(over=generator)` → `GROUP BY snapshot`, labels → `ROW_NUMBER()` over the
  masked coord product, LP writing → per-section `COPY` under a duckdb
  `memory_limit` with a file-backed database, sections concatenated at the end.
- `check_equivalence.py` — differential test: solve both LP files with HiGHS,
  assert equal dimensions and objective.
- `bench.py` — gate benchmark via pytest-benchmem (memray under the hood,
  `isolate=True` → per-pass fresh process + `ru_maxrss` peak RSS).

## Run

```bash
uv sync --extra dev --group spike

# differential correctness
uv run --group spike python scratch/relational_spike/gen_data.py --snapshots 20000 --generators 50 --out /tmp/d
uv run --group spike python scratch/relational_spike/linopy_baseline.py --data /tmp/d --out /tmp/a.lp
uv run --group spike python scratch/relational_spike/duckdb_spike.py --data /tmp/d --out /tmp/b.lp
uv run --group spike python scratch/relational_spike/check_equivalence.py /tmp/a.lp /tmp/b.lp

# gate benchmark
uv run --group spike python scratch/relational_spike/bench.py --snapshots 100000 --generators 100 --repeats 3
```

## Results (macOS, M-series, 24 GB RAM, linopy 0.9.0 / duckdb 1.5.5)

Peak RSS via `/usr/bin/time -l`, untracked (see "measurement pitfalls" below).
Dispatch model, G=100 (89 active after mask).

| model | backend | budget | peak RSS | wall | LP size |
|---|---|---|---|---|---|
| S=100k (8.9M vars) | linopy lp-polars | — | 2.26 GB | 1.6–1.9 s | 555 MB |
| S=100k | duckdb | 1 GB / chunk 25k | 0.81 GB | 3.1 s | 637 MB |
| S=100k | duckdb | 256 MB / chunk 25k | 0.73 GB | 3.2 s | 637 MB |
| S=400k (35.6M vars) | linopy lp-polars | — | 6.60 GB | 7.8 s | 2.31 GB |
| S=400k | duckdb | 512 MB / chunk 25k | **0.49 GB** | 14.5 s | 2.64 GB |
| S=400k | duckdb | 256 MB / chunk 25k | 0.49 GB | 18.9 s | 2.64 GB |
| S=400k | duckdb | 2 GB / chunk 100k | 1.19 GB | 16.3 s | 2.64 GB |
| S=1.2M (107M vars) | linopy lp-polars | — | (~20 GB extrapolated — exceeds this machine) | — | — |
| S=1.2M | duckdb | 512 MB / chunk 25k | 0.57 GB | 74 s | 7.99 GB |

**Gate verdict:** peak RSS win is decisive (13.6× at 35.6M vars) and flat in
model size — the config knob works. The 107M-var run is the capability proof:
a model whose dense build cannot fit on this machine streams out at 0.57 GB. Runtime is ~2× slower than linopy's
lp-polars writer and does *not* improve with a bigger budget: it's dominated
by fixed work (re-scanning the vars table per section, ~100M `printf` calls,
2.6 GB CSV write), not by spilling. linopy's RSS grows linearly with the dense
product (2.26 GB → 6.60 GB for 4×); duckdb's stays at the budget.

Differential check (HiGHS solve, objective + dimensions): EQUIVALENT at 1.6k,
920k, and 8.9M variables (re-checked after every plan change; at 8.9M the
objectives agree to 14 significant digits).

## Phase-2 follow-up: the general executor matches the hand-written SQL

`executor_bench.py` runs the same dispatch model through the phase-2 IR +
`DuckdbExecutor` (`linopy_yaml/relational/`). At S=100k, G=100 under a 512 MB
budget: build 3.1 s (8.9M cols, 100k rows), `write_lp` 2.0 s (637 MB, same
file size as the spike), 0.84 GB peak RSS — no generality penalty vs the
hand-written SQL (3.1 s, 0.81 GB). End-to-end `solver_direct` (batched HiGHS
`addCols`/`addRows`, no LP file): solve 30.5 s, Optimal, objective identical
to the differential oracle; process peak 5.76 GB is dominated by HiGHS's own
model + simplex workspace — the irreducible floor — while the build side
stays at the configured budget.

## What the spike learned (feeds phase-2 IR design)

1. **Not every relational operator spills.** duckdb raises OOM rather than
   exceed `memory_limit` when the plan needs an unspillable operator:
   - `string_agg(... ORDER BY ...)` (ordered aggregate) buffers everything.
     Plain `string_agg` is fine at moderate group counts.
   - A global-`ORDER BY` window (`ROW_NUMBER` for labels) materializes its
     whole input — OOMs at ~35M rows under tight budgets.
   - Even a plain external-sort rewrite of the constraint section OOMs below
     ~1 GB at 9.3M rows.
2. **Partition-wise execution is the fix, and belongs in the IR executor.**
   Chunking by the leading foreach dim (snapshot ranges) bounds every operator:
   labels = per-chunk ROW_NUMBER + running offset; constraint blocks =
   per-chunk GROUP BY + COPY, parts concatenated. Chunk size × memory limit
   trade off directly; 25k snapshots fits in 256 MB.
3. **LP section order is free.** Line order inside sections is irrelevant to
   solvers (labels live in the text) → no global sorts needed;
   `SET preserve_insertion_order=false`.
4. **Measurement pitfalls:** memray's tracker slows duckdb ~8× (allocation
   interception on a multithreaded allocation-heavy engine) while barely
   affecting linopy — never benchmark *runtime* under memray. memray's
   allocator peak also overcounts polars (reserved arenas). Use untracked
   `/usr/bin/time -l` peak RSS as the gate metric; memray only for attribution.
5. **A file-backed duckdb database** (not `:memory:`) is required for the
   buffer pool to spill table data under `memory_limit`.
6. **Primary invariant: the full model is never held in our process's memory** —
   not as dense arrays, not as a full CSR. Every stage streams; the solver's
   internal copy is the only irreducible full-model residency.
7. **Sinks are a first-class IR concept** (`lp_file`, `mps`, `solver_direct`).
   The end-state sink streams COO/CSR straight to the solver: the post-GROUP-BY
   expression table is already `(row, col, coeff)`, and dense ROW_NUMBER labels
   are the solver column/row indices — no remapping pass. Stream via
   `ORDER BY row` + Arrow record batches split on row boundaries (Gurobi
   `addMConstr` per CSR chunk, HiGHS `addRows` per batch). Peak becomes
   `memory_limit` + one Arrow chunk + the solver's own model (the irreducible
   floor), and float→text→parse disappears. The LP-file sink stays as the
   debugging/portability oracle and the apples-to-apples benchmark vs
   lp-polars. Solution read-back: primal/dual arrays are indexed by label —
   join against the label tables, straight to parquet.
