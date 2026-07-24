# Measured results: streaming vs eager LP construction

The evidence for the central claim — that build peak RSS is a config knob
(`memory_limit`) rather than O(dense dim product).

These numbers were produced by a benchmark harness under `scratch/` that has
since been removed: it had to be re-run by hand, drifted from the shipped
code, and its correctness arm is now the differential test suite, which runs
on every commit. The results are kept here because the conclusion still
governs the architecture; reproducing them means writing a fresh harness
against `linopy_yaml.api`, and the pitfalls below are what to watch for.

## Results (macOS, M-series, 24 GB RAM, linopy 0.9.0 / duckdb 1.5.5)

Peak RSS via `/usr/bin/time -l`, untracked (see measurement pitfalls below).
Dispatch model (`examples/dispatch.yaml`), G=100 (89 active after mask).

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

**Gate verdict:** the peak-RSS win is decisive (13.6× at 35.6M vars) and flat
in model size — the config knob works. The 107M-var run is the capability
proof: a model whose dense build cannot fit on this machine streams out at
0.57 GB. Runtime is ~2× slower than linopy's lp-polars writer and does *not*
improve with a bigger budget — it is dominated by fixed work (re-scanning the
vars table per section, ~100M `printf` calls, 2.6 GB CSV write), not by
spilling. linopy's RSS grows linearly with the dense product (2.26 GB → 6.60 GB
for 4×); duckdb's stays at the budget.

Differential check (HiGHS solve, objective + dimensions): equivalent at 1.6k,
920k, and 8.9M variables; at 8.9M the objectives agree to 14 significant
digits.

## The general executor matches the hand-written SQL

The same dispatch model through the IR + `DuckdbExecutor` at S=100k, G=100
under a 512 MB budget: build 3.1 s (8.9M cols, 100k rows), `write_lp` 2.0 s
(637 MB — same file size as the hand-written spike SQL), 0.84 GB peak RSS.
No generality penalty against the hand-written version (3.1 s, 0.81 GB).

End-to-end `solver_direct` (batched HiGHS `addCols`/`addRows`, no LP file):
solve 30.5 s, Optimal, objective identical to the oracle. Process peak
5.76 GB is dominated by HiGHS's own model + simplex workspace — the
irreducible floor — while the build side stays at the configured budget.

## Operational findings

These are load-bearing for `relational/executor.py`; its module docstring
states the resulting rules, and this is the evidence behind them.

1. **Not every relational operator spills.** duckdb raises OOM rather than
   exceed `memory_limit` when the plan needs an unspillable operator:
   - `string_agg(... ORDER BY ...)` (ordered aggregate) buffers everything.
     Plain `string_agg` is fine at moderate group counts.
   - A global-`ORDER BY` window (`ROW_NUMBER` for labels) materialises its
     whole input — OOMs at ~35M rows under tight budgets.
   - Even a plain external-sort rewrite of the constraint section OOMs below
     ~1 GB at 9.3M rows.
2. **Partition-wise execution is the fix.** Chunking by the leading foreach
   dim bounds every operator: labels = per-chunk `ROW_NUMBER` + running
   offset; constraint blocks = per-chunk `GROUP BY` + `COPY`, parts
   concatenated. Chunk size and memory limit trade off directly; 25k
   snapshots fits in 256 MB.
3. **LP section order is free.** Line order inside sections is irrelevant to
   solvers (labels live in the text), so no global sorts are needed —
   hence `SET preserve_insertion_order=false`.
4. **Measurement pitfalls.** memray's tracker slows duckdb ~8× (allocation
   interception on a multithreaded, allocation-heavy engine) while barely
   affecting linopy — never benchmark *runtime* under memray. memray's
   allocator peak also overcounts polars (reserved arenas). Use untracked
   `/usr/bin/time -l` peak RSS as the gate metric; memray only for
   attribution.
5. **A file-backed duckdb database** (not `:memory:`) is required for the
   buffer pool to spill table data under `memory_limit`.
