# Measured results: relational vs eager LP construction

**Cost lives here, not in the architecture's rules.** What a build costs is a
property of the engine and it is settled by measurement; the rules in
`ARCHITECTURE.md` constrain the language and would survive an engine swap
untouched. That separation is why this file can be rewritten by a benchmark run
without anything in `SPEC.md` moving.

**Build memory alone is not the claim.** It is a small fraction of what solving
costs, so shrinking it further changes nothing a caller feels — the tables below
are the input to two claims that do, and both live further down: end-to-end peak
against the floor under the LP-file route, and marginal cost per model in a loop.

Peak RSS and wall time for the same model built two ways — declaratively on the
relational engine, and eagerly through linopy — from the same parquet files to
the same LP file. Produced by [`bench/`](../bench/README.md) and read straight
off [`bench/results/latest.jsonl`](../bench/results/latest.jsonl), which carries
the machine fingerprint and library versions that produced it:

```bash
uv run python -m bench.run --cases dispatch nodal transport --sizes xs s m l
uv run python -m bench.report bench/results/latest.jsonl
```

macOS, M-series, 26 GB. python 3.13.2 · polars 1.43.0 · linopy 0.9.0 ·
highspy 1.15.1. Parity gate: all three cases agree with the eager lane to
0.0e+00 relative before anything is timed.

## Results

`wall` and `peak` are farkas ÷ linopy: **below 1.00 is a win for us.**

### dispatch — pointwise bounds, one `sum` per row

| variables | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak |
|---|---|---|---|---|---|---|
| 10k | 0.09 s | 0.30 s | **0.30x** | 0.17 GB | 0.21 GB | **0.82x** |
| 100k | 0.15 s | 0.31 s | **0.47x** | 0.23 GB | 0.26 GB | **0.86x** |
| 1M | 0.44 s | 0.53 s | **0.83x** | 0.55 GB | 0.58 GB | **0.95x** |
| 10M | 4.64 s | 3.25 s | 1.43x | 2.78 GB | 2.26 GB | 1.23x |

### nodal — `(snapshot, node, tech)` at 25% density

| variables | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak |
|---|---|---|---|---|---|---|
| 3k | 0.09 s | 0.31 s | **0.30x** | 0.17 GB | 0.21 GB | **0.81x** |
| 30k | 0.10 s | 0.32 s | **0.33x** | 0.21 GB | 0.24 GB | **0.90x** |
| 300k | 0.32 s | 0.41 s | **0.79x** | 0.46 GB | 0.46 GB | 1.00x |
| 3M | 2.44 s | 1.50 s | 1.63x | 1.93 GB | 1.54 GB | 1.25x |

### sector — dense snapshots and carriers, sparse portfolio

50 nodes x 12 technologies at 8% installed, crossed with 5 dense carriers. `p`
is sparse in `(node, tech)`; the balance is dense in time and carrier;
`sum(p * produces, over=tech)` broadcasts a dense dim onto a sparse frame and
then reduces the sparse one.

| variables | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak |
|---|---|---|---|---|---|---|
| 1k | 0.09 s | 0.32 s | **0.28x** | 0.17 GB | 0.21 GB | **0.81x** |
| 10k | 0.10 s | 0.33 s | **0.31x** | 0.20 GB | 0.24 GB | **0.85x** |
| 100k | 0.18 s | 0.33 s | **0.56x** | 0.41 GB | 0.53 GB | **0.79x** |
| 1M | 1.47 s | 1.39 s | 1.05x | **1.24 GB** | 2.97 GB | **0.42x** |

### transport — three `group_sum` joins per row

| variables | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak |
|---|---|---|---|---|---|---|
| 9.8k | 0.10 s | 0.32 s | **0.31x** | 0.18 GB | 0.22 GB | **0.83x** |
| 98k | 0.14 s | 0.36 s | **0.40x** | 0.25 GB | 0.28 GB | **0.87x** |
| 980k | 0.54 s | 0.64 s | **0.85x** | 0.70 GB | 0.65 GB | 1.09x |
| 9.8M | 6.71 s | 3.71 s | 1.81x | 2.87 GB | 1.93 GB | 1.48x |

## What this says

**Below ~1M variables we win on both axes, by 2–3x on wall time.** That is the
range declarative modelling is actually used in, and the range a rolling horizon
lives in entirely.

**Above ~1M the build-side memory advantage narrows or inverts.** At 10M
variables the LP path is level on `dispatch` (1.06x) and 2.1x on `transport`:
COO carries a `(row, col, coeff)` triple per nonzero where a dense array carries
one float. **Read the next section before drawing a conclusion from that** —
measured to the point a caller actually reaches, we are ahead on all three, and
build memory on its own is not a number anyone experiences.

`transport` is the case still paying full price, because three `group_sum`
fragments land on every row and so the terminal aggregate cannot be skipped
(see `_needs_aggregate`). It is the obvious next thing to look at.

**Labels are a position, not a count.** An unmasked coordinate product needs no
sort: a row's label is arithmetic on the dim ordinals, so it is computed rather
than counted. That path cut `transport`'s build from 2.57 s to 0.87 s and
`dispatch`'s from 0.97 s to 0.49 s at the `l` rung — the largest single win in
this file, and one that has nothing to do with which engine executes it.

**What is left at scale is the LP writer and the terminal aggregate.** The
writer is the larger share; the aggregate over every nonzero is the other, and
it is a no-op for most constraints — [#161](https://github.com/FBumann/farkas/issues/161)
measures skipping it at ~30% of build peak and roughly 3x build time. It is not
done here deliberately: eliding it is a correctness-critical change that has
earned its own review rather than an engine swap's coat-tails.

**Sparsity separates them, but only once the coordinate product is large.**
The `sector` row above is the clearest result in this file — 2.2x less memory
at 1M live variables out of a 12M product — and the density sweep below shows
why it took two cases to find.

## The density sweep, and a claim it refuses

One model size (50 nodes x 12 technologies x 2000 snapshots = 1.2M coordinates),
four mask densities. The expectation was that an absent pair costs the
relational lane nothing and costs the eager lane a NaN, so the gap should widen
as density falls.

| installed | live vars | wall: farkas | wall: linopy | peak: farkas | peak: linopy |
|---|---|---|---|---|---|
| 12/12 | 1.20M | 0.58 s | 0.59 s | 0.68 GB | 0.63 GB |
| 6/12 | 0.60M | **0.38 s** | 0.45 s | 0.52 GB | 0.60 GB |
| 3/12 | 0.30M | **0.35 s** | 0.41 s | 0.46 GB | 0.45 GB |
| 1/12 | 0.10M | **0.19 s** | 0.32 s | 0.40 GB | **0.35 GB** |

**Not here it does not.** linopy's peak falls with density too — 0.63 to
0.35 GB — and at the sparsest rung it ends up *below* ours.

The reason is the size this sweep is run at, not the prediction. It holds the
coordinate product fixed at 1.2M, where a dense array over it is ~10 MB and the
interpreter and libraries dominate everything. `sector` runs the same 8%
sparsity at a 12M product, and there the effect is unmistakable: 1.27 GB against
2.79 GB.

So the claim needs both halves — **low density and a product large enough for it
to cost anything**. This sweep varies one at a size that cannot show it; `sector`
varies the other. Neither is sufficient alone, which is worth knowing before
quoting either.

Wall time behaves throughout: our advantage grows as the model thins, 1.0x to
1.7x, because there is less to build and our fixed cost is lower.

## Build and hand off — the number a user actually pays

The tables above stop at an LP file, which is one of two sinks and the one
fewer people use. `solver_direct` hands the COO straight to HiGHS, and **the
handoff is part of the cost this package controls** — so it belongs in the
comparison. Measured with `Highs.run` stubbed out, which keeps the simplex
workspace (which no engine here influences) out of the number while keeping
HiGHS's own copy of the model, which the handoff necessarily creates.

Same machine, `l` rung of each case:

| | build | + handoff | handoff time |
|---|---|---|---|
| **dispatch, 10M** | | | |
| farkas | 1.44 GB | **3.13 GB** | 3.55 s |
| linopy (`io_api='direct'`) | 0.75 GB | 3.38 GB | 2.92 s |
| **nodal, 3M @ 25% density** | | | |
| farkas | 1.21 GB | **1.50 GB** | **0.89 s** |
| linopy (`io_api='direct'`) | 1.04 GB | 2.01 GB | 1.26 s |
| **transport, 9.8M** | | | |
| farkas | 2.95 GB | **3.71 GB** | 2.69 s |
| linopy (`io_api='direct'`) | 1.06 GB | 4.04 GB | 2.31 s |

**Measured here we are ahead on all three** — 3.13 against 3.38 GB, 1.50
against 2.01, 3.71 against 4.04 — where the LP-file tables above have us behind
on two of them. HiGHS's own model is the larger term on both sides, so a
build-side deficit that looks decisive at the LP file mostly is not where a
caller stands.

**The sparse case is the widest margin**: 1.50 GB against 2.01, in two thirds
of the time. `nodal` is the shape real multi-node models have, which makes it
the row to weight.

**Do not read `io_api='lp'` numbers as the eager lane's cost.** The same
`dispatch` model through linopy's LP-file path peaks at 6.92 GB and takes 55 s
to hand off, against 3.38 GB and 2.9 s direct. The tables above compare against
linopy's *better* path deliberately.

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

Against a `solver_direct` peak already dominated by HiGHS's own model, that is
a small fraction. On `lp_file` a quadratic
objective is a text section and sinks like any other. So this is a cost, not an
invariant violation. Two caveats:

- HiGHS accepts `dim_ < num_col` (verified), so ordering quadratic variables
  first bounds the Hessian to that block rather than the whole model.
- **The diagonal argument dies with the aligned restriction.** General bilinear
  `Q` is not diagonal, and its cost stops tracking the model — a second,
  independent reason that restriction is load-bearing.

## Method

Recorded in [`bench/README.md`](../bench/README.md) — one process per
measurement, `ru_maxrss` rather than a tracker, import excluded from
`wall_seconds` and teardown included, and a parity gate that aborts the run
before anything is timed if the two lanes disagree. Failures are results and are
rendered as cells.

Measurement pitfall worth keeping: memray's tracker slows an allocation-heavy
engine several-fold and overcounts reserved arenas, so it can attribute memory
but must never time anything. Peak RSS is the gate metric; memray is for
attribution only.
