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
the same LP file.

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
uv run python -m bench.run --cases dispatch nodal sector transport --sizes xs s m l
uv run python -m bench.report bench/results/latest.jsonl
```

macOS, M-series, 26 GB. python 3.13.2 · polars 1.43.0 · linopy 0.9.0 ·
highspy 1.15.1. Parity gate: all four cases agree with the eager lane to
0.0e+00 relative before anything is timed.

## Results

`wall` and `peak` are farkas ÷ linopy: **below 1.00 is a win for us.**

### dispatch — pointwise bounds, one `sum` per row

| variables | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak |
|---|---|---|---|---|---|---|
| 10k | 0.05 s | 0.21 s | **0.25x** | 0.17 GB | 0.21 GB | **0.79x** |
| 100k | 0.06 s | 0.22 s | **0.29x** | 0.21 GB | 0.26 GB | **0.81x** |
| 1M | 0.20 s | 0.35 s | **0.57x** | 0.48 GB | 0.59 GB | **0.82x** |
| 10M | 1.62 s | 1.73 s | **0.94x** | 2.42 GB | 2.19 GB | 1.11x |

### nodal — `(snapshot, node, tech)` at 25% density

| variables | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak |
|---|---|---|---|---|---|---|
| 3k | 0.05 s | 0.21 s | **0.24x** | 0.17 GB | 0.21 GB | **0.80x** |
| 30k | 0.06 s | 0.22 s | **0.27x** | 0.20 GB | 0.24 GB | **0.86x** |
| 300k | 0.12 s | 0.27 s | **0.43x** | 0.41 GB | 0.46 GB | **0.90x** |
| 3M | 0.78 s | 0.85 s | **0.92x** | 1.37 GB | 1.56 GB | **0.88x** |

### sector — dense snapshots and carriers, sparse portfolio

50 nodes x 12 technologies at 8% installed, crossed with 5 dense carriers. `p`
is sparse in `(node, tech)`; the balance is dense in time and carrier;
`sum(p * produces, over=tech)` broadcasts a dense dim onto a sparse frame and
then reduces the sparse one.

| variables | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak |
|---|---|---|---|---|---|---|
| 1k | 0.05 s | 0.22 s | **0.24x** | 0.17 GB | 0.21 GB | **0.80x** |
| 10k | 0.06 s | 0.22 s | **0.26x** | 0.20 GB | 0.24 GB | **0.83x** |
| 100k | 0.10 s | 0.30 s | **0.34x** | 0.39 GB | 0.52 GB | **0.73x** |
| 1M | 0.55 s | 1.09 s | **0.51x** | **0.97 GB** | 2.96 GB | **0.33x** |

### transport — three `group_sum` joins per row

| variables | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak |
|---|---|---|---|---|---|---|
| 9.8k | 0.06 s | 0.23 s | **0.24x** | 0.18 GB | 0.22 GB | **0.81x** |
| 98k | 0.07 s | 0.25 s | **0.29x** | 0.24 GB | 0.29 GB | **0.82x** |
| 980k | 0.23 s | 0.41 s | **0.56x** | 0.62 GB | 0.67 GB | **0.93x** |
| 9.8M | 2.18 s | 2.09 s | 1.04x | 3.24 GB | 1.87 GB | 1.73x |

## What this says

**Below ~1M variables we win on both axes, by 2–3x on wall time.** That is the
range declarative modelling is actually used in, and the range a rolling horizon
lives in entirely.

**Above ~1M the build-side memory advantage narrows or inverts.** At 10M
variables the LP path is 1.11x on `dispatch` and 1.73x on `transport`.
**Read the next section before drawing a conclusion from that** — measured to
the point a caller actually reaches, we are ahead on all three, and build
memory on its own is not a number anyone experiences.

**What that peak is made of is not yet known, and the obvious answer is
wrong.** The tempting explanation is COO — a `(row, col, coeff)` triple per
nonzero where a dense array carries one float. Accounted at the `l` rung on
`transport`, it is not the story: against a 2.58 GB peak at the end of the
build, the four model frames are 0.77 GB of which the matrix itself is 0.30,
the variable label frames are 0.22, and **1.48 GB is unaccounted for**. COO is
under a tenth of the peak it was being used to explain.

`transport` is the case that pays this, and the standing hypothesis is that
three `group_sum` fragments land on every row, so the terminal aggregate
cannot be skipped (see `_needs_aggregate`) and its intermediates are what the
allocator has not returned. That is consistent with `transport` being the only
case with this profile, but it is a hypothesis and not a measurement — the
unaccounted gigabyte is the next thing to isolate, and no number here should
be quoted as its cause until someone has.

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

Emit is now **0.81x, 0.79x, 0.39x and 0.96x** of linopy's writer at the `l`
rung — ahead on every case, where before it was behind on three. Peak did not
move: this was wall time, and it leaves the memory rows exactly where they
were. **`transport`'s 1.73x peak is now the one open number at scale.**

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

## The density sweep, and a claim it refuses

One model size (50 nodes x 12 technologies x 2000 snapshots = 1.2M coordinates),
four mask densities. The expectation was that an absent pair costs the
relational lane nothing and costs the eager lane a NaN, so the gap should widen
as density falls.

| installed | live vars | wall: farkas | wall: linopy | peak: farkas | peak: linopy |
|---|---|---|---|---|---|
| 12/12 | 1.20M | **0.26 s** | 0.39 s | 0.60 GB | 0.62 GB |
| 6/12 | 0.60M | **0.16 s** | 0.31 s | 0.46 GB | 0.60 GB |
| 3/12 | 0.30M | **0.12 s** | 0.27 s | 0.40 GB | 0.45 GB |
| 1/12 | 0.10M | **0.09 s** | 0.25 s | 0.36 GB | **0.35 GB** |

**Not here it does not.** linopy's peak falls with density too — 0.62 to
0.35 GB — and at the sparsest rung it ends up *below* ours.

The reason is the size this sweep is run at, not the prediction. It holds the
coordinate product fixed at 1.2M, where a dense array over it is ~10 MB and the
interpreter and libraries dominate everything. `sector` runs the same 8%
sparsity at a 12M product, and there the effect is unmistakable: 0.97 GB against
2.96 GB.

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
comparison.

`l` rung of each case, one process per arm, best of two:

| | build | + handoff | = total | peak |
|---|---|---|---|---|
| **dispatch, 10M** | | | | |
| farkas | 0.45 s | 0.56 s | **1.01 s** | 3.28 GB |
| linopy (`io_api='direct'`) | 0.26 s | 1.70 s | 1.96 s | 3.39 GB |
| **nodal, 3M @ 25% density** | | | | |
| farkas | 0.35 s | 0.17 s | **0.52 s** | 1.50 GB |
| linopy (`io_api='direct'`) | 0.34 s | 0.78 s | 1.12 s | 1.97 GB |
| **transport, 9.8M** | | | | |
| farkas | 0.75 s | 0.76 s | **1.53 s** | 3.55 GB |
| linopy (`io_api='direct'`) | 0.66 s | 1.98 s | 2.65 s | 3.70 GB |

**What is in the number**

| | |
|---|---|
| Counted, both arms | reading the parquet · building the model · assembling the matrix · loading it into HiGHS |
| Not counted, both arms | `import` · the simplex — `Highs.run` is stubbed and returns without solving · reading the solution back |
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

**Measured to here we are about 2x faster on all three** — 0.51x, 0.46x, 0.57x
— against roughly level on the LP-file tables above. Nothing about the engine
changes between the two tables; the difference is that the LP-file route
spends most of its clock turning doubles into text and writing them, which is
work neither lane can avoid and which therefore compresses the ratio toward
1.00 however fast the build was.

**Peak is the weaker half of this claim.** 3.28 against 3.39 GB and 3.55
against 3.70 are inside the run-to-run spread — `transport`'s linopy arm read
3.14 GB on its other repeat, i.e. ahead of us — so the honest reading is
*level* on those two. Only `nodal`, at 1.50 against 1.97, separates. HiGHS's
own copy dominates once the model is loaded, and neither lane can shrink it.

**The sparse case is the one to weight**: `nodal` is the shape real multi-node
models have, and it is the widest margin on both axes — half the time and three
quarters of the memory.

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
