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

Linux 6.18.5 x86-64 · python 3.11.15 · farkas 0.0.0a13, linopy 0.9.0, duckdb
1.5.5, polars 1.43.0, xarray 2026.7.0. Parity gate: all three cases agree to 0
relative (bit-identical objectives). Best of two repeats; wall time excludes
import.

*These replace an earlier set measured on macOS/M-series. Do not compare the
two: different hardware, and the label frame changed underneath them.*

### dispatch — pointwise bounds + one `sum` per row

| variables | rows | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|
| 10k | 100 | 0.18 s | 0.28 s | 0.65x | 0.18 GB | 0.23 GB | 0.78x | 1 MB |
| 100k | 1k | 0.41 s | 0.39 s | 1.04x | 0.20 GB | 0.28 GB | 0.74x | 7 MB |
| 1M | 10k | 0.99 s | 1.04 s | 0.95x | 0.31 GB | 0.67 GB | 0.46x | 74 MB |
| 10M | 100k | 10.08 s | 10.08 s | 1.00x | 0.62 GB | 2.17 GB | 0.28x | 767 MB |

### sparse — the same math behind a 2-D `where` that keeps ~20%

| variables | rows | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|
| 2.065k | 100 | 0.15 s | 0.30 s | 0.49x | 0.18 GB | 0.23 GB | 0.79x | 0 MB |
| 20.888k | 1k | 0.28 s | 0.36 s | 0.77x | 0.19 GB | 0.25 GB | 0.76x | 1 MB |
| 208.416k | 10k | 0.81 s | 0.77 s | 1.05x | 0.23 GB | 0.50 GB | 0.47x | 15 MB |
| 2.08094M | 100k | 3.74 s | 6.54 s | 0.57x | 0.44 GB | 2.24 GB | 0.20x | 159 MB |

### transport — three `group_sum` joins per row

| variables | rows | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|
| 9.8k | 1.4k | 0.23 s | 0.39 s | 0.60x | 0.18 GB | 0.23 GB | 0.80x | 1 MB |
| 98k | 14k | 0.52 s | 0.51 s | 1.02x | 0.22 GB | 0.30 GB | 0.72x | 7 MB |
| 980k | 140k | 1.56 s | 1.10 s | 1.41x | 0.35 GB | 0.74 GB | 0.48x | 76 MB |
| 9.8M | 1.4M | 11.25 s | 9.31 s | 1.21x | 1.00 GB | 2.08 GB | 0.48x | 794 MB |

### Peak RSS against the budget

farkas at three `memory_limit` settings, 16x apart end to end:

| case | variables | `256MB` | `1GB` | `4GB` | linopy |
|---|---|---|---|---|---|
| dispatch | 10k | 0.18 GB | 0.18 GB | 0.18 GB | 0.23 GB |
| dispatch | 100k | 0.20 GB | 0.20 GB | 0.21 GB | 0.28 GB |
| dispatch | 1M | 0.31 GB | 0.31 GB | 0.31 GB | 0.67 GB |
| dispatch | 10M | 0.52 GB | 0.62 GB | 0.61 GB | 2.17 GB |
| sparse | 2.065k | 0.18 GB | 0.18 GB | 0.18 GB | 0.23 GB |
| sparse | 20.888k | 0.19 GB | 0.19 GB | 0.19 GB | 0.25 GB |
| sparse | 208.416k | 0.23 GB | 0.23 GB | 0.24 GB | 0.50 GB |
| sparse | 2.08094M | 0.44 GB | 0.44 GB | 0.46 GB | 2.24 GB |
| transport | 9.8k | 0.19 GB | 0.18 GB | 0.18 GB | 0.23 GB |
| transport | 98k | 0.22 GB | 0.22 GB | 0.22 GB | 0.30 GB |
| transport | 980k | 0.35 GB | 0.35 GB | 0.35 GB | 0.74 GB |
| transport | 9.8M | 0.57 GB | 1.00 GB | 0.99 GB | 2.08 GB |

## What the numbers say

**The wall-time gap is gone on two cases of three.** dispatch is 1.00x at 10M
variables and transport 1.21x; the earlier "~2x slower over 10⁶ variables" was
real, but it was one statement. Build was 6–10x slower while emit was already at
parity, and inside build the cost was the sort in the label frame's
`ROW_NUMBER() OVER (ORDER BY …)`. Where the mask does not read the leading dim
that sort is avoidable at identical labels, which is what closed it. transport
is the one still behind, and its `group_sum` fan-in is where to look next.

**Masks are the qualitative axis, and they were not being measured.**
`dispatch` masks on `p_max > 0` against a p_max that is always positive, so its
`where` removes nothing — the ladder measured a fully dense coord product, which
is the eager lane's best case and ours worst. The `sparse` case keeps ~20% of a
2-D product and the shape inverts: **0.57x wall and 0.20x peak** at 2.1M live
variables. Row absence costs what the surviving rows cost; NaN padding costs
what the dense product costs.

**The memory win widens with the model, as before.** 0.78x at 10k is a fixed
import floor. At the top rung it is 0.28x on dispatch, 0.48x on transport, 0.20x
on sparse.

**Peak still does not track the budget.** Across 16x of `memory_limit`, peak at
dispatch/10M moves 0.52 → 0.61 GB and transport/9.8M 0.57 → 1.00 GB. Whatever
dominates is outside duckdb's accounting (LP text buffers, Arrow batches, the
label tables), so hard rule 4 holds in its *comparative* form — peak flat in
model size relative to the eager lane — and not in its literal one. Wall time is
likewise budget-insensitive.

**The transport OOM did not reproduce.** The earlier run died at 9.8M under
256 MB in the terminal `SUM(coeff) GROUP BY row, col`; here it completes at
0.57 GB. Different machine *and* a changed label frame, so this is not evidence
the shape is fixed — it is evidence the failure is marginal, and it should be
re-run on the machine that saw it before the finding is retired.

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

Runtime there was attributed to fixed emit-side work — re-scanning the vars
table per section, ~100M `printf` calls, a 2.6 GB CSV write. The phase timings
above say that attribution was wrong: emit was already at parity with linopy's
writer and the gap was entirely in build. The conclusion it supported — that a
bigger budget does not help — still holds, for the different reason that the
cost was a sort, not spilling. End-to-end `solver_direct` (batched HiGHS
`addCols`/`addRows`, no LP file) at 35.6M variables: solve 30.5 s, Optimal,
objective identical to the oracle; process peak 5.76 GB dominated by HiGHS's own
model and simplex workspace, the residency hard rule 4 exempts.

## Not measured yet

In rough order of what would change a decision: `solver_direct` end-to-end (the
shipped path, where the LP file is not written at all); `storage` (`roll`, the
bounded-halo self-join); a MILP, where solve time dwarfs build; and a
hand-written highspy/CSR arm as the speed-of-light floor, without which a ratio
against linopy has no denominator.

The `where`-density axis has moved off this list — it is the `sparse` case
above, and it was the one that mattered: it was predicted to be "qualitative
rather than 2×" and it is, in our favour. A *sweep* over density is still worth
having; one point is not a curve.

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
