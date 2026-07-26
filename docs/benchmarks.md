# Measured results: streaming vs eager LP construction

Two questions, both measured rather than asserted: what the streaming lane buys
against an eager build, and what `memory_limit` actually does.

The harness is [`bench/memory.py`](../bench/memory.py). It runs every
configuration in a fresh subprocess and reports that process's own
`ru_maxrss` — peak RSS, untracked, the gate metric finding 4 explains:

```bash
uv run python bench/memory.py --snapshots 100000 400000 --eager --sink lp
uv run python bench/memory.py --budgets 128MB 256MB 512MB 1GB 2GB
```

Numbers below: Linux, 4 cores, 15 GB RAM, python 3.11.15, duckdb 1.5.5,
linopy 0.9.0. Dispatch model (`examples/dispatch.yaml` widened), G=100 (89
active after the `p_max > 0` mask), `chunk_rows=25k`. Sources are parquet paths
written by a separate process, so no input array is resident in a measured one.

## The floor

| phase | peak RSS |
|---|---:|
| bare interpreter | 13 MiB |
| `import farkas` | 33 MiB |
| duckdb loaded, connection open, no model | **81 MiB** |

81 MiB at a 128MB budget and at a 2GB budget alike. `memory_limit` governs
duckdb's buffer manager and has no opinion about the interpreter or the shared
objects, so this floor is a constant the budget cannot move. Every figure below
is quoted against it.

## What the budget does

Build only, no sink.

| budget | 8.9M vars | vs budget | 35.6M vars | vs budget |
|---|---:|---:|---:|---:|
| 128MB | 419 MiB | 3.28x | **OOM** | — |
| 256MB | 501 MiB | 1.96x | 573 MiB | 2.24x |
| 512MB | 653 MiB | 1.27x | 772 MiB | 1.51x |
| 1GB | 628 MiB | 0.61x | 1,214 MiB | 1.19x |
| 2GB | 633 MiB | 0.31x | 1,868 MiB | 0.91x |

**`memory_limit` is a lever, not a ceiling.** It moves peak monotonically and
substantially — 128MB to 512MB is a 1.56x swing at 8.9M variables — but the
process overshoots the configured limit by up to 3.3x at tight settings, and
subtracting the 81 MiB floor does not rescue it (338 MiB over a 128MB budget is
still 2.6x). The excess is untracked allocation inside duckdb. Threads are only
a small part of it: at 8.9M variables under 128MB, 1 thread peaks at 364 MiB,
2 at 369 MiB, 4 at 415 MiB — single-threaded still overshoots 2.85x.

**It saturates.** At 8.9M variables, 512MB / 1GB / 2GB all land near 630-650
MiB: once the working set fits, a larger budget buys nothing. At 35.6M
variables nothing saturates below 2GB, because the working set is larger than
any of these budgets lets it reach. Peak therefore tracks whichever binds
first:

```
peak ~ 81 MiB + min(working set, a small multiple of the budget)
```

which is why a budget that binds makes peak look flat in model size (at 512MB,
a 4x model costs 1.18x the memory) and a budget that does not makes it look
proportional (at 2GB, 2.95x).

**Too tight is a failure, not a slower build.** 35.6M variables under 128MB
raises `OutOfMemoryException` from the `A` assembly — see finding 2.

## Streaming vs eager

Same model, same box, same sink on both arms, `memory_limit=512MB`.

| model | sink | eager (linopy) | streaming | ratio |
|---|---|---:|---:|---:|
| 8.9M vars | build only | 759 MiB | 666 MiB | 1.14x |
| 8.9M vars | + LP write | 2,152 MiB | 661 MiB | **3.3x** |
| 35.6M vars | build only | 2,341 MiB | 780 MiB | 3.0x |
| 35.6M vars | + LP write | 6,280 MiB | 774 MiB | **8.1x** |

**Give both arms the same sink.** A build-only eager number and a
build-plus-write streaming number are not comparable, and the difference is not
small: adding the LP write costs the streaming lane nothing measurable
(666 -> 661, 780 -> 774, both inside run-to-run noise) and nearly triples eager
peak (759 -> 2,152, 2,341 -> 6,280). linopy's `lp-polars` writer materialises
what the streaming sink drains in batches. That contrast is the architecture's
claim stated directly, and it is a better result than any single ratio.

**The win grows with the model.** For a 4x larger model, eager peak grows 2.9x
and streaming peak grows 1.17x. The ratio is 3.3x at 8.9M variables and 8.1x at
35.6M.

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
2. **The `A` assembly is a third member of that family, not an exception.**
   `_build_constraint`'s single-shot `INSERT INTO A ... GROUP BY row, col` is
   documented as needing no chunking because a plain numeric hash aggregate
   spills on its own. Measured, it does not spill far enough: 35.6M variables
   under a 128MB budget dies there with `could not allocate block of size
   256.0 KiB (121.9 MiB/122.0 MiB used)`. It is the one operator that scales
   with the model rather than with a chunk, so it sets the floor on how low a
   budget can go. Chunking it by the leading foreach dim — the mechanism label
   assignment already uses — is the fix.
3. **Partition-wise execution is the fix.** Chunking by the leading foreach dim
   bounds every operator: labels = per-chunk `ROW_NUMBER` + running offset;
   constraint blocks = per-chunk `GROUP BY` + `COPY`, parts concatenated. Chunk
   size and memory limit trade off directly; 25k snapshots fits in 256 MB.
4. **LP section order is free.** Line order inside sections is irrelevant to
   solvers (labels live in the text), so no global sorts are needed — hence
   `SET preserve_insertion_order=false`.
5. **Measurement pitfalls.** memray's tracker slows duckdb ~8x (allocation
   interception on a multithreaded, allocation-heavy engine) while barely
   affecting linopy — never benchmark *runtime* under memray, and its allocator
   peak overcounts polars (reserved arenas). Use untracked peak RSS as the gate
   metric; memray for attribution only. Measure the floor separately: a
   whole-process metric cannot judge a subsystem's budget without it.
6. **A file-backed duckdb database** (not `:memory:`) is required for the buffer
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

Against a `solver_direct` peak dominated by HiGHS's own model, which hard rule 4
exempts, that is a modest addition. On `lp_file`, where nothing is exempt, a
quadratic objective is a text section and streams like any other. So this is a
cost, not an invariant violation. Two caveats:

- HiGHS accepts `dim_ < num_col` (verified), so ordering quadratic variables
  first bounds the Hessian to that block rather than the whole model.
- **The diagonal argument dies with the aligned restriction.** General bilinear
  `Q` is not diagonal, and then peak stops tracking the budget — a second,
  independent reason that restriction is load-bearing.
