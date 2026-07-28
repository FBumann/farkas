# Measured results: relational vs eager LP construction

**Cost lives here, not in the architecture's rules.** What a build costs is a
property of the engine and it is settled by measurement; the rules in
`ARCHITECTURE.md` constrain the language and would survive an engine swap
untouched. That separation is why this file can be rewritten by a benchmark run
without anything in `SPEC.md` moving.

**Build memory alone is not the claim.** It is a small fraction of what solving
costs, so shrinking it further changes nothing a caller feels. What the tables
below settle is the claim a caller does feel: **cost to a loaded solver**, which
is [Build and hand off](#build-and-hand-off--the-number-a-user-actually-pays) —
wall *and* peak, on the sink most callers reach for, against the eager lane's
own best path to the same place. The LP-file columns are the secondary route,
and they answer a different question; read the sink you actually use.

Two things this file does **not** measure, both named in [Not measured
yet](#not-measured-yet): the floor under the LP-file route as a cold cost
(writing a file and reading it back), and marginal cost per model in a loop.
Neither is quoted here, and neither should be quoted from here.

Peak RSS and wall time for the same model built two ways — declaratively on the
relational engine, and eagerly through linopy — from the same parquet files to
the same destination.

**The eager arm is `farkas.linopy.build`, not hand-written linopy.** It is our
own YAML→`linopy.Model` shim, so it carries our loader on top of linopy's work
and the ratios below are therefore kind to us. Measured on `dispatch/m`, the
same 1M-variable model written by hand against the shim: build 0.31 s against
0.36 s, with emit identical because it is the same `Model.to_file` on the same
object. So read roughly 15% off the eager arm's build to get what a linopy user
would see — enough to move a ratio, not enough to move a conclusion.

Produced by [`bench/`](../bench/README.md) and read straight off
[`bench/results/latest.jsonl`](../bench/results/latest.jsonl), which carries
the machine fingerprint and library versions that produced it:

```bash
uv run python -m bench.run --sizes xs s m l d100 d50 d25 d08
uv run python -m bench.report bench/results/latest.jsonl
```

macOS, M-series, 26 GB. python 3.13.2 · polars 1.43.0 · linopy 0.9.0 ·
highspy 1.15.1. Parity gate: all four cases agree with the eager lane to
0.0e+00 relative before anything is timed.

## Results

`wall` and `peak` are farkas ÷ linopy: **below 1.00 is a win for us.**

**Two sinks, and they are not the same comparison.** The LP file is the
artifact fewest callers want; `highs` is the one most reach for, and there
HiGHS's own dense model is resident in both arms. Read the sink you
actually use — the ratios differ by more than the noise between them.

### dispatch — highs sink

Both arms end holding a populated `highspy.Highs` with `run()` never called: farkas through `build_highs`, linopy through `to_highspy()`. The simplex is the same work whoever filled the model, so timing it would say nothing about the lane that filled it.

| variables | live | rows | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 10k | 100% | 100 | 0.02 s | 0.20 s | 0.08x | 0.17 GB | 0.20 GB | 0.83x | — |
| 100k | 100% | 1k | 0.02 s | 0.22 s | 0.11x | 0.21 GB | 0.23 GB | 0.91x | — |
| 1M | 100% | 10k | 0.09 s | 0.38 s | 0.24x | 0.52 GB | 0.49 GB | 1.05x | — |
| 10M | 100% | 100k | 0.71 s | 1.99 s | 0.36x | 3.14 GB | 3.30 GB | 0.95x | — |

### dispatch — lp sink

farkas writes the LP file, linopy through its `lp-polars` writer.

| variables | live | rows | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 10k | 100% | 100 | 0.01 s | 0.20 s | 0.07x | 0.17 GB | 0.21 GB | 0.79x | 1 MB |
| 100k | 100% | 1k | 0.03 s | 0.23 s | 0.12x | 0.21 GB | 0.26 GB | 0.81x | 7 MB |
| 1M | 100% | 10k | 0.13 s | 0.37 s | 0.34x | 0.47 GB | 0.59 GB | 0.80x | 76 MB |
| 10M | 100% | 100k | 1.20 s | 1.58 s | 0.76x | 2.26 GB | 2.24 GB | 1.01x | 796 MB |

### nodal — highs sink

Both arms end holding a populated `highspy.Highs` with `run()` never called: farkas through `build_highs`, linopy through `to_highspy()`. The simplex is the same work whoever filled the model, so timing it would say nothing about the lane that filled it.

| variables | live | rows | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 3k | 25% | 1k | 0.02 s | 0.21 s | 0.09x | 0.17 GB | 0.20 GB | 0.83x | — |
| 30k | 25% | 10k | 0.02 s | 0.22 s | 0.10x | 0.19 GB | 0.21 GB | 0.87x | — |
| 300k | 25% | 100k | 0.05 s | 0.29 s | 0.16x | 0.34 GB | 0.36 GB | 0.95x | — |
| 3M | 25% | 1M | 0.36 s | 1.11 s | 0.32x | 1.43 GB | 1.78 GB | 0.81x | — |

### nodal — lp sink

farkas writes the LP file, linopy through its `lp-polars` writer.

| variables | live | rows | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 3k | 25% | 1k | 0.02 s | 0.21 s | 0.08x | 0.17 GB | 0.21 GB | 0.79x | 0 MB |
| 30k | 25% | 10k | 0.02 s | 0.22 s | 0.09x | 0.19 GB | 0.24 GB | 0.81x | 2 MB |
| 300k | 25% | 100k | 0.07 s | 0.26 s | 0.26x | 0.32 GB | 0.45 GB | 0.71x | 25 MB |
| 3M | 25% | 1M | 0.50 s | 0.81 s | 0.61x | 1.29 GB | 1.57 GB | 0.82x | 264 MB |

### profiled — highs sink

Both arms end holding a populated `highspy.Highs` with `run()` never called: farkas through `build_highs`, linopy through `to_highspy()`. The simplex is the same work whoever filled the model, so timing it would say nothing about the lane that filled it.

| variables | live | rows | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 12k | 100% | 1k | 0.02 s | 0.21 s | 0.09x | 0.18 GB | 0.20 GB | 0.86x | — |
| 120k | 100% | 10k | 0.04 s | 0.23 s | 0.17x | 0.27 GB | 0.25 GB | 1.10x | — |
| 1.2M | 100% | 100k | 0.27 s | 0.43 s | 0.63x | 0.91 GB | 0.64 GB | 1.42x | — |
| 12M | 100% | 1M | 2.91 s | 2.58 s | 1.13x | 4.37 GB | 4.47 GB | 0.98x | — |

### profiled — lp sink

farkas writes the LP file, linopy through its `lp-polars` writer.

| variables | live | rows | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 12k | 100% | 1k | 0.02 s | 0.21 s | 0.09x | 0.18 GB | 0.22 GB | 0.80x | 1 MB |
| 120k | 100% | 10k | 0.04 s | 0.23 s | 0.19x | 0.27 GB | 0.30 GB | 0.89x | 9 MB |
| 1.2M | 100% | 100k | 0.32 s | 0.38 s | 0.83x | 0.77 GB | 0.72 GB | 1.07x | 95 MB |
| 12M | 100% | 1M | 3.43 s | 1.93 s | 1.78x | 3.62 GB | 3.19 GB | 1.14x | 986 MB |

### sector — highs sink

Both arms end holding a populated `highspy.Highs` with `run()` never called: farkas through `build_highs`, linopy through `to_highspy()`. The simplex is the same work whoever filled the model, so timing it would say nothing about the lane that filled it.

| variables | live | rows | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 1k | 6% | 1k | 0.02 s | 0.23 s | 0.09x | 0.17 GB | 0.20 GB | 0.83x | — |
| 10k | 6% | 10k | 0.02 s | 0.23 s | 0.10x | 0.19 GB | 0.22 GB | 0.87x | — |
| 100k | 6% | 100k | 0.05 s | 0.32 s | 0.15x | 0.34 GB | 0.50 GB | 0.69x | — |
| 1M | 6% | 1M | 0.31 s | 1.22 s | 0.26x | 0.94 GB | 3.01 GB | 0.31x | — |

### sector — lp sink

farkas writes the LP file, linopy through its `lp-polars` writer.

| variables | live | rows | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 1k | 6% | 1k | 0.02 s | 0.22 s | 0.08x | 0.17 GB | 0.21 GB | 0.79x | 0 MB |
| 10k | 6% | 10k | 0.02 s | 0.23 s | 0.10x | 0.19 GB | 0.24 GB | 0.79x | 1 MB |
| 100k | 6% | 100k | 0.06 s | 0.31 s | 0.18x | 0.34 GB | 0.53 GB | 0.65x | 12 MB |
| 1M | 6% | 1M | 0.39 s | 1.05 s | 0.37x | 0.92 GB | 2.97 GB | 0.31x | 120 MB |

### transport — highs sink

Both arms end holding a populated `highspy.Highs` with `run()` never called: farkas through `build_highs`, linopy through `to_highspy()`. The simplex is the same work whoever filled the model, so timing it would say nothing about the lane that filled it.

| variables | live | rows | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 9.8k | 100% | 1.4k | 0.02 s | 0.23 s | 0.11x | 0.17 GB | 0.20 GB | 0.85x | — |
| 98k | 100% | 14k | 0.03 s | 0.25 s | 0.13x | 0.22 GB | 0.23 GB | 0.95x | — |
| 980k | 100% | 140k | 0.15 s | 0.47 s | 0.32x | 0.60 GB | 0.56 GB | 1.08x | — |
| 9.8M | 100% | 1.4M | 1.33 s | 2.57 s | 0.52x | 3.14 GB | 3.89 GB | 0.81x | — |

### transport — lp sink

farkas writes the LP file, linopy through its `lp-polars` writer.

| variables | live | rows | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 9.8k | 100% | 1.4k | 0.02 s | 0.23 s | 0.11x | 0.18 GB | 0.22 GB | 0.81x | 1 MB |
| 98k | 100% | 14k | 0.04 s | 0.25 s | 0.15x | 0.23 GB | 0.29 GB | 0.81x | 8 MB |
| 980k | 100% | 140k | 0.21 s | 0.42 s | 0.48x | 0.55 GB | 0.66 GB | 0.83x | 79 MB |
| 9.8M | 100% | 1.4M | 2.04 s | 2.07 s | 0.99x | 3.02 GB | 1.90 GB | 1.59x | 820 MB |

## What this says

**Below ~1M variables we win on both axes, by 2–3x on wall time.** That is the
range declarative modelling is actually used in, and the range a rolling horizon
lives in entirely.

**The sink decides the answer, and `transport`'s peak is the clearest case.**
Through the LP writer it is 1.59x — the number this file has called the one
open problem at scale. Through the hand-off, the sink most callers actually
use, the same model at the same rung is **0.81x**. Nothing about the build
changed between those two columns: the LP path spends its peak turning doubles
into text, while HiGHS's own dense model is resident in both arms and dwarfs
the difference between the lanes that filled it.

So the honest reading is per sink. At the `l` rung, wall and peak:

| | lp | highs |
|---|---|---|
| dispatch | 0.76x / 1.01x | **0.36x** / **0.95x** |
| nodal | 0.61x / 0.82x | **0.32x** / **0.81x** |
| sector | 0.37x / 0.31x | **0.26x** / 0.31x |
| transport | 0.99x / **1.59x** | **0.52x** / **0.81x** |
| profiled | 1.78x / 1.14x | 1.13x / **0.98x** |

**We are ahead on both axes on four of five cases through the hand-off**, and
the one we are not is `profiled`.

**The obvious explanation for that peak was wrong.** COO — a
`(row, col, coeff)` triple per nonzero where a dense array carries one float —
is under a tenth of it: accounted at the `l` rung on `transport`, the matrix
frame is 0.30 GB of a peak measured in gigabytes. A memory timeline through
the build put the peak in one call, `_build_constraint`, which arrived
carrying +1.27 GB of it.

**A third of that was an aggregate collapsing nothing.** `_needs_aggregate`
reads the term fragments, so it can only answer whether a cell *can* repeat;
`transport` stacks three, so the answer is yes on every row. At the `l` rung it
took 12.6M entries in and returned 12.6M — building a hash table with one group
per row to collapse zero duplicates. Sorting first and reading the answer off
adjacent pairs costs less than the aggregate did (0.44 s against 0.57 s) and
builds the table only when there is something to collapse. That is what moved
this row from 1.73x to 1.51x, and it halved the *transient* — peak above what
the build leaves resident — from 0.70-0.86 GB to 0.34-0.35 GB.

**What is left is resident, not transient, and still unexplained.** After the
change, `transport` holds ~1.86 GB at the end of the build against 0.77 GB of
model frames and 0.22 GB of variable label frames, so roughly 0.76 GB is
neither. The label frames are droppable on a write-only path and worth about
0.1x of the ratio; the rest has no candidate yet. **No number here should be
quoted as its cause** — that is the mistake this paragraph replaced.

It also matters less than it looked. That accounting was taken on the LP path,
where the 1.59x lives; through the hand-off the same build reads 0.81x. The
gigabyte is worth finding, but it is not what stands between this lane and the
sink most callers use.

**`profiled` is the case we lose, and it is in the ladder to be lost.** Its
`availability` parameter is dense over the whole variable product — a 12M-row
table against a 12M coordinate product — which is exactly the array xarray
wants, needing neither broadcast nor alignment, while we join a full-size frame
against a full-size product. 1.78x on wall through the LP writer, 1.13x through
the hand-off, and peak better than linopy's on the hand-off (0.98x) despite it.
A ladder holding only shapes that suit one engine proves nothing, which is why
this one is here.

**Labels are a position, not a count.** An unmasked coordinate product needs no
sort: a row's label is arithmetic on the dim ordinals, so it is computed rather
than counted. That path cut `transport`'s build from 2.57 s to 0.87 s and
`dispatch`'s from 0.97 s to 0.49 s at the `l` rung — the largest single win in
this file, and one that has nothing to do with which engine executes it.

**The LP writer is no longer what is left at scale.** It was: emit is most of
the wall clock at the `l` rung on every case, and the writer used to lose to
linopy's on three of the four. Two changes closed it, and neither was a better
relational plan.

The first was that the sink wrote every byte twice — each section to a part
file, then a pass concatenating the parts. Sections come out in the order the
LP format wants them, so the parts bought nothing, and at 800 MB the
concatenation costs more than producing the text did. Sinking each section
straight into the open file took **30-35% off emit on all four cases** —
measured as a before/after on one machine, with the old sink re-measured
afterwards to confirm the machine had not drifted under the pair.

The second was that a line was built by chaining `+`, which allocates a
full-width string column per operator, where one `concat_str` allocates the
line once. In the same pass the sign stopped being decided in front of a
rendered `abs()` — that renders the magnitude in both arms of the `when` to
discard one, and the cast already carries the `-`.

Emit is now ahead of linopy's writer on every case at the `l` rung, where
before it was behind on three. Neither change touches memory: they are wall
time, and the peak rows moved for a different reason (the aggregate, above).

The earlier change in this area, emitting one sorted stream of lines rather
than gathering each row's terms into a string, is what makes the bytes
reproducible (#109), which is what it was for.

**Ratios, not seconds.** Both arms of a row are measured in the same run under
the same machine load and the report takes the fastest of three, so the ratio
columns survive a busy machine in a way the absolute times do not. This table
was taken on a quiet one — nothing else above 40% CPU — because a competing
job moved the *farkas* arm more than the linopy arm and so moved the ratio
too.

**Sparsity separates them, but only once the coordinate product is large.**
The `sector` row above is the clearest result in this file — 3x less memory
at 1M live variables out of a 12M product — and the density sweep below shows
why it took two cases to find.

## The density sweep, and the claim it used to refuse

One model size (50 nodes x 12 technologies x 2000 snapshots = 1.2M coordinates),
four mask densities. The expectation was that an absent pair costs the
relational lane nothing and costs the eager lane a NaN, so the gap should widen
as density falls.

| case | live | variables | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak |
|---|---|---|---|---|---|---|---|---|
| nodal | 100% | 1.2M | 0.17 s | 0.38 s | 0.45x | 0.58 GB | 0.64 GB | 0.90x |
| nodal | 50% | 600k | 0.10 s | 0.32 s | 0.32x | 0.41 GB | 0.60 GB | 0.69x |
| nodal | 25% | 300k | 0.06 s | 0.27 s | 0.24x | 0.32 GB | 0.45 GB | 0.71x |
| nodal | 8% | 100k | 0.04 s | 0.26 s | 0.17x | 0.26 GB | 0.35 GB | 0.74x |

**It now does, and it did not before.** Wall time falls from 0.45x to 0.17x as
density drops, and peak improves at every rung — 0.87x, 0.70x, 0.71x, 0.74x.
The previous run of this sweep had linopy's peak *below* ours at the sparsest
rung, and the note here said so.

What changed is not the prediction but what a mask costs us. Assigning labels
under a mask used to mean sorting the whole masked product; a mask that reads
none of the leading dims now leaves a rectangle, so the labels are arithmetic
and the sort is of the surviving *set*. That was 46-66% of the build on every
masked case. The sweep was measuring our own cost of being sparse, and most of
it is gone.

The remaining caveat on the *memory* half stands, and it is the size this sweep
is run at rather than the prediction. It holds the
coordinate product fixed at 1.2M, where a dense array over it is ~10 MB and the
interpreter and libraries dominate everything. `sector` runs the same 8%
sparsity at a 12M product, and there the effect is unmistakable: 0.92 GB against
2.97 GB.

So the claim needs both halves — **low density and a product large enough for it
to cost anything**. This sweep varies one at a size that cannot show it; `sector`
varies the other. Neither is sufficient alone, which is worth knowing before
quoting either.

Wall time behaves throughout: our advantage grows as the model thins, 1.0x to
1.7x, because there is less to build and our fixed cost is lower.

## Build and hand off — the number a user actually pays

**This section used to hold a hand-measured table with no script behind it.**
It is now the `highs` column of the Results tables above, produced by the same
harness as everything else — `bench/run.py --sinks highs`, one process per
measurement, best of three, parity-gated. The note that used to live here,
saying the peak column was stale and could not be re-run, is gone because the
table it applied to is gone.

`solver_direct` hands the COO straight to HiGHS, and **the hand-off is part of
the cost this package controls** — so it belongs in the comparison. It is also
the sink most callers reach for, which is why both sinks now run by default
rather than the LP file alone.

**The measurement stops before `run()`.** `build_highs` is the seam, and
linopy's `to_highspy()` is the same seam on the other side: both arms end
holding a populated `highspy.Highs` and neither solves it. The simplex is the
same work whoever filled the model, so timing it would swamp the phase this
harness exists to measure and publish a number about HiGHS under our name.

**What is in the number**

| | |
|---|---|
| Counted, both arms | reading the parquet · building the model · assembling the matrix · loading it into HiGHS |
| Not counted, both arms | `import` · the simplex — the hand-off stops before `run()` · reading the solution back |
| Only one arm pays | *farkas*: routing paths to parameters vs dimensions sits outside the clock; it parses the YAML and reads no data. *linopy*: nothing. |
| Known tilts, both toward us | the eager arm is `farkas.linopy.build`, ~15% slower to build than hand-written linopy · peak carries each lane's import footprint, ~40 MB heavier for linopy |

**Read the total, not the phases.** linopy's direct path calls `model.matrices`
at *solve* time, so it assembles and `tocsr()`s the matrix inside its handoff,
where this lane produced COO during its build. The phase columns are shown so
that split is visible, not so either can be quoted alone: the handoff column on
its own flatters us by exactly the work we did earlier, and the build column
flatters linopy by the same amount.

`import` is out because it is ~0.07 s of polars against ~0.20 s of linopy +
xarray + pandas — a fixed cost of the lane, not of the model.

**Measured to here we are 2-4x faster on four of five cases** — 0.36x, 0.32x,
0.26x, 0.52x, with `profiled` at 1.13x — against a narrower spread on the LP
path. Nothing about the engine changes between the two sinks; the difference is
that the LP route spends most of its clock turning doubles into text and
writing them, which is work neither lane can avoid and which therefore
compresses the ratio toward
1.00 however fast the build was.

**Peak is the weaker half of this claim, and it is the half that moved.** On
the hand-off we are now ahead on all five — 0.95x, 0.81x, 0.31x, 0.81x, 0.98x —
where the previous measurement read *level* on two of three. HiGHS's own copy
dominates once the model is loaded and neither lane can shrink it, so these
margins are narrower than the wall ones and will stay that way: `dispatch` at
0.95x is a real result, not a rounding one, but it is not `sector`'s 0.32x
either.

**The sparse case is the one to weight**: `nodal` is the shape real multi-node
models have, and it is among the widest margins on both axes — a third of the
time and four fifths of the memory.

**Do not read `io_api='lp'` numbers as the eager lane's cost.** The same
`dispatch` model through linopy's LP-file path peaks at 6.92 GB and takes 55 s
to hand off, against 3.38 GB and 1.7 s direct. The tables above compare against
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

## Not measured yet

This section exists so that a claim with no table under it is visible as one.
Two of its entries are load-bearing elsewhere — `README.md` and `ROADMAP.md`
lead on cost, and until these land they lead on the hand-off numbers above and
nothing else.

In rough order of what would change a decision:

- **The LP-file route as a cold floor.** The hand-off tables compare against
  linopy's *best* path deliberately. What they do not price is the route the
  claim "there is no file" is really about: write the LP, then have a solver
  read it back. The one figure in that direction is anecdotal and single-case —
  `dispatch/l` through linopy's `io_api='lp'` peaks at 6.92 GB against 3.38 GB
  direct — and it prices only the *writing* half, in the eager lane.
- **Marginal cost per model in a loop.** The architectural claim is that
  nothing accumulates between builds, so the hundredth rolling-horizon window
  costs what the first did. It follows from there being no process-wide state
  and no lifetime to leak, and every rung here is a single build in a fresh
  process — which is exactly why none of them tests it.
- **`storage` — `roll`, the bounded-halo self-join.** The one plan shape in the
  language whose cost is not obviously linear in the model, and no case
  exercises it.
- **A MILP**, where solve time dwarfs build and the build ratio stops mattering.
- **A hand-written highspy/CSR arm** as the speed-of-light floor. Without one,
  every ratio here has linopy as its only denominator.

Two entries that used to be here are now measured and have moved into the file:
`solver_direct` end to end (the `highs` sink, which now runs by default) and the
mask-density sweep.

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
