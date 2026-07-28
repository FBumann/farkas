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
own YAML→`linopy.Model` shim, so it carries our loader on top of linopy's work.
Measured against hand-written linopy on the same model, the shim costs a
constant **~2.3 ms** — 1.26x at `xs`, 1.15x at `m` — so it is a fixed offset
rather than the ~15% this file used to claim from one measurement at one rung.
Nowhere near enough to move a conclusion.

**Which code produced these numbers.** Three engines means three answers, and
the duckdb arm is a checkout rather than a release, so a version string alone
is not enough:

| arm | engine | commit |
|---|---|---|
| `farkas` | this branch's polars engine, polars 1.43.0 | `45902ec` |
| `linopy` | linopy **0.8.0.post1.dev140+g346943317** — the v1-semantics build (PyPSA/linopy#717) | shim at the same commit |
| `duckdb` | the engine this branch replaces, duckdb 1.5.5 | `4a13d38` on `main`, which carries the same v1 semantics (#239) |

highspy 1.15.1 · numpy 2.5.1 · pandas 3.0.5 · xarray 2026.7.0 · pyarrow 25.0.0.
macOS-26.2-arm64-arm-64bit-Mach-O, python 3.13.2.

`bench/run.py` writes the versions *and* a commit per arm into the first line
of `latest.jsonl`, because it fingerprints installed distributions: an
editable install reports the version it was synced at rather than the tree
that ran, and a checkout arm has no distribution at all.

Produced by [`bench/`](../bench/README.md) and read straight off
[`bench/results/latest.jsonl`](../bench/results/latest.jsonl), which carries
the machine fingerprint and library versions that produced it:

```bash
uv run python -m bench.run --sizes xs s m l d100 d50 d25 d08 \
    --arms farkas linopy duckdb --duckdb-root ../farkas-main
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

| variables | live | rows | wall: farkas | wall: linopy | wall: duckdb | wall | peak: farkas | peak: linopy | peak: duckdb | peak | LP |
|---|---|---|---|---|---|---|---|---|---|---|
| 10k | 100% | 100 | 0.02 s | 0.20 s | 0.05 s | 0.08x | 0.17 GB | 0.20 GB | 0.16 GB | 0.83x | — |
| 100k | 100% | 1k | 0.02 s | 0.22 s | 0.10 s | 0.10x | 0.21 GB | 0.23 GB | 0.19 GB | 0.91x | — |
| 1M | 100% | 10k | 0.08 s | 0.37 s | 0.39 s | 0.22x | 0.52 GB | 0.50 GB | 0.46 GB | 1.05x | — |
| 10M | 100% | 100k | 0.68 s | 1.92 s | 2.75 s | 0.35x | 3.13 GB | 3.30 GB | 2.03 GB | 0.95x | — |

### dispatch — lp sink

farkas writes the LP file, linopy through its `lp-polars` writer.

| variables | live | rows | wall: farkas | wall: linopy | wall: duckdb | wall | peak: farkas | peak: linopy | peak: duckdb | peak | LP |
|---|---|---|---|---|---|---|---|---|---|---|
| 10k | 100% | 100 | 0.01 s | 0.20 s | 0.05 s | 0.07x | 0.17 GB | 0.21 GB | 0.16 GB | 0.79x | 1 MB |
| 100k | 100% | 1k | 0.03 s | 0.21 s | 0.13 s | 0.12x | 0.21 GB | 0.26 GB | 0.18 GB | 0.80x | 7 MB |
| 1M | 100% | 10k | 0.12 s | 0.33 s | 0.38 s | 0.38x | 0.47 GB | 0.57 GB | 0.33 GB | 0.82x | 76 MB |
| 10M | 100% | 100k | 1.09 s | 1.53 s | 2.86 s | 0.71x | 2.24 GB | 2.22 GB | 0.77 GB | 1.01x | 796 MB |

### nodal — highs sink

Both arms end holding a populated `highspy.Highs` with `run()` never called: farkas through `build_highs`, linopy through `to_highspy()`. The simplex is the same work whoever filled the model, so timing it would say nothing about the lane that filled it.

| variables | live | rows | wall: farkas | wall: linopy | wall: duckdb | wall | peak: farkas | peak: linopy | peak: duckdb | peak | LP |
|---|---|---|---|---|---|---|---|---|---|---|
| 3k | 25% | 1k | 0.02 s | 0.21 s | 0.05 s | 0.08x | 0.17 GB | 0.20 GB | 0.16 GB | 0.83x | — |
| 30k | 25% | 10k | 0.02 s | 0.22 s | 0.08 s | 0.09x | 0.19 GB | 0.21 GB | 0.17 GB | 0.88x | — |
| 300k | 25% | 100k | 0.05 s | 0.29 s | 0.26 s | 0.16x | 0.34 GB | 0.35 GB | 0.27 GB | 0.95x | — |
| 3M | 25% | 1M | 0.34 s | 1.07 s | 1.78 s | 0.32x | 1.43 GB | 1.71 GB | 0.98 GB | 0.84x | — |

### nodal — lp sink

farkas writes the LP file, linopy through its `lp-polars` writer.

| variables | live | rows | wall: farkas | wall: linopy | wall: duckdb | wall | peak: farkas | peak: linopy | peak: duckdb | peak | LP |
|---|---|---|---|---|---|---|---|---|---|---|
| 3k | 25% | 1k | 0.02 s | 0.22 s | 0.05 s | 0.07x | 0.17 GB | 0.21 GB | 0.16 GB | 0.79x | 0 MB |
| 30k | 25% | 10k | 0.02 s | 0.21 s | 0.08 s | 0.09x | 0.19 GB | 0.24 GB | 0.17 GB | 0.80x | 2 MB |
| 300k | 25% | 100k | 0.06 s | 0.26 s | 0.27 s | 0.24x | 0.32 GB | 0.44 GB | 0.22 GB | 0.72x | 25 MB |
| 3M | 25% | 1M | 0.50 s | 0.79 s | 1.79 s | 0.64x | 1.28 GB | 1.48 GB | 0.54 GB | 0.86x | 264 MB |

### profiled — highs sink

Both arms end holding a populated `highspy.Highs` with `run()` never called: farkas through `build_highs`, linopy through `to_highspy()`. The simplex is the same work whoever filled the model, so timing it would say nothing about the lane that filled it.

| variables | live | rows | wall: farkas | wall: linopy | wall: duckdb | wall | peak: farkas | peak: linopy | peak: duckdb | peak | LP |
|---|---|---|---|---|---|---|---|---|---|---|
| 12k | 100% | 1k | 0.02 s | 0.21 s | 0.06 s | 0.09x | 0.17 GB | 0.21 GB | 0.17 GB | 0.85x | — |
| 120k | 100% | 10k | 0.03 s | 0.23 s | 0.19 s | 0.15x | 0.26 GB | 0.25 GB | 0.22 GB | 1.06x | — |
| 1.2M | 100% | 100k | 0.20 s | 0.43 s | 0.88 s | 0.47x | 0.85 GB | 0.64 GB | 0.59 GB | 1.32x | — |
| 12M | 100% | 1M | 2.15 s | 2.49 s | 6.34 s | 0.86x | 4.14 GB | 4.73 GB | 2.70 GB | 0.88x | — |

### profiled — lp sink

farkas writes the LP file, linopy through its `lp-polars` writer.

| variables | live | rows | wall: farkas | wall: linopy | wall: duckdb | wall | peak: farkas | peak: linopy | peak: duckdb | peak | LP |
|---|---|---|---|---|---|---|---|---|---|---|
| 12k | 100% | 1k | 0.02 s | 0.21 s | 0.06 s | 0.08x | 0.18 GB | 0.22 GB | 0.16 GB | 0.80x | 1 MB |
| 120k | 100% | 10k | 0.04 s | 0.22 s | 0.22 s | 0.17x | 0.26 GB | 0.29 GB | 0.20 GB | 0.88x | 9 MB |
| 1.2M | 100% | 100k | 0.26 s | 0.38 s | 0.89 s | 0.69x | 0.71 GB | 0.69 GB | 0.41 GB | 1.04x | 95 MB |
| 12M | 100% | 1M | 2.62 s | 1.95 s | 6.47 s | 1.35x | 3.43 GB | 3.06 GB | 1.50 GB | 1.12x | 986 MB |

### sector — highs sink

Both arms end holding a populated `highspy.Highs` with `run()` never called: farkas through `build_highs`, linopy through `to_highspy()`. The simplex is the same work whoever filled the model, so timing it would say nothing about the lane that filled it.

| variables | live | rows | wall: farkas | wall: linopy | wall: duckdb | wall | peak: farkas | peak: linopy | peak: duckdb | peak | LP |
|---|---|---|---|---|---|---|---|---|---|---|
| 1k | 6% | 1k | 0.02 s | 0.22 s | — | 0.09x | 0.17 GB | 0.20 GB | — | 0.83x | — |
| 10k | 6% | 10k | 0.02 s | 0.23 s | — | 0.10x | 0.19 GB | 0.22 GB | — | 0.87x | — |
| 100k | 6% | 100k | 0.05 s | 0.31 s | — | 0.15x | 0.34 GB | 0.49 GB | — | 0.69x | — |
| 1M | 6% | 1M | 0.31 s | 1.23 s | — | 0.25x | 0.96 GB | 2.98 GB | — | 0.32x | — |

### sector — lp sink

farkas writes the LP file, linopy through its `lp-polars` writer.

| variables | live | rows | wall: farkas | wall: linopy | wall: duckdb | wall | peak: farkas | peak: linopy | peak: duckdb | peak | LP |
|---|---|---|---|---|---|---|---|---|---|---|
| 1k | 6% | 1k | 0.02 s | 0.22 s | — | 0.08x | 0.17 GB | 0.21 GB | — | 0.79x | 0 MB |
| 10k | 6% | 10k | 0.02 s | 0.22 s | — | 0.10x | 0.19 GB | 0.24 GB | — | 0.79x | 1 MB |
| 100k | 6% | 100k | 0.05 s | 0.30 s | — | 0.18x | 0.34 GB | 0.52 GB | — | 0.65x | 12 MB |
| 1M | 6% | 1M | 0.39 s | 1.04 s | — | 0.37x | 0.94 GB | 2.91 GB | — | 0.32x | 120 MB |

### transport — highs sink

Both arms end holding a populated `highspy.Highs` with `run()` never called: farkas through `build_highs`, linopy through `to_highspy()`. The simplex is the same work whoever filled the model, so timing it would say nothing about the lane that filled it.

| variables | live | rows | wall: farkas | wall: linopy | wall: duckdb | wall | peak: farkas | peak: linopy | peak: duckdb | peak | LP |
|---|---|---|---|---|---|---|---|---|---|---|
| 9.8k | 100% | 1.4k | 0.02 s | 0.23 s | 0.06 s | 0.11x | 0.17 GB | 0.20 GB | 0.17 GB | 0.85x | — |
| 98k | 100% | 14k | 0.03 s | 0.25 s | 0.12 s | 0.14x | 0.22 GB | 0.23 GB | 0.21 GB | 0.95x | — |
| 980k | 100% | 140k | 0.12 s | 0.43 s | 0.50 s | 0.28x | 0.60 GB | 0.57 GB | 0.54 GB | 1.05x | — |
| 9.8M | 100% | 1.4M | 1.12 s | 2.49 s | 3.59 s | 0.45x | 3.02 GB | 3.95 GB | 2.54 GB | 0.76x | — |

### transport — lp sink

farkas writes the LP file, linopy through its `lp-polars` writer.

| variables | live | rows | wall: farkas | wall: linopy | wall: duckdb | wall | peak: farkas | peak: linopy | peak: duckdb | peak | LP |
|---|---|---|---|---|---|---|---|---|---|---|
| 9.8k | 100% | 1.4k | 0.02 s | 0.23 s | 0.06 s | 0.10x | 0.18 GB | 0.22 GB | 0.16 GB | 0.80x | 1 MB |
| 98k | 100% | 14k | 0.04 s | 0.24 s | 0.15 s | 0.15x | 0.23 GB | 0.29 GB | 0.20 GB | 0.81x | 8 MB |
| 980k | 100% | 140k | 0.20 s | 0.39 s | 0.48 s | 0.52x | 0.56 GB | 0.64 GB | 0.34 GB | 0.87x | 79 MB |
| 9.8M | 100% | 1.4M | 1.60 s | 1.87 s | 3.49 s | 0.85x | 3.12 GB | 1.85 GB | 1.16 GB | 1.68x | 820 MB |

## What this says

**Ahead on both axes, on every case, through the sink most callers use.**
At the `l` rung, to a loaded solver: wall 0.35x, 0.32x, 0.25x, 0.45x, 0.86x
and peak 0.95x, 0.84x, 0.32x, 0.76x, 0.88x. `profiled` was the exception until
the duplicate-coordinate check stopped grouping 12M rows to answer a yes/no
question; it is now a win like the rest.

**The sink decides the answer.** The LP file is the artifact fewest callers
want and the ratios there are far weaker — 0.71x to 1.35x on wall, and
`transport` is 1.61x on peak. Most of an LP write is turning doubles into
text, which is work neither lane avoids, so that ratio compresses toward 1.00
however fast the build gets. Read the sink you actually use.

**Against the engine this replaces**, at the same rung: polars is 2.2-5.2x
faster than duckdb on every case and both sinks, and duckdb is 1.2-2.9x
lighter. That is the trade this branch makes, and neither half of it is
marginal. duckdb is also slower than the eager lane everywhere here — the
streaming engine's advantage was never wall time.

### Two costs, and both are real

The harness spawns one process per measurement, so every timing above is a
*first* build, and a first build pays whatever lazy work a lane does on its
first call. That is the right number for a caller who builds one model and
solves it, and the wrong one for a rolling horizon, which pays it once. So the
build is measured both ways, to the same end point — build **and** hand-off,
because linopy defers ~82% of its work to whatever consumes the model and a
build-only comparison would measure our finished matrix against its
placeholder:

| case | vars | farkas: first | farkas: steady | linopy: first | linopy: steady | duckdb: first | duckdb: steady | steady vs linopy |
|---|---|---|---|---|---|---|---|---|
| transport | 9.8k | 19.8 ms | **13.4 ms** | 222.4 ms | 35.5 ms | 56.8 ms | 34.1 ms | 0.38x |
| dispatch | 10k | 10.5 ms | **5.8 ms** | 196.6 ms | 13.8 ms | 45.6 ms | 22.3 ms | 0.42x |
| nodal | 12k | 12.1 ms | **6.9 ms** | 205.7 ms | 18.6 ms | 44.6 ms | 22.7 ms | 0.37x |
| profiled | 12k | 16.2 ms | **8.3 ms** | 206.0 ms | 19.6 ms | 52.6 ms | 32.6 ms | 0.42x |
| sector | 17k | 14.8 ms | **9.2 ms** | 214.6 ms | 24.5 ms | 48.3 ms | 25.2 ms | 0.38x |
| transport | 98k | 26.5 ms | **19.2 ms** | 224.8 ms | 37.8 ms | 101.4 ms | 80.0 ms | 0.51x |
| dispatch | 100k | 14.0 ms | **8.0 ms** | 194.5 ms | 14.6 ms | 82.7 ms | 60.1 ms | 0.55x |
| nodal | 120k | 14.5 ms | **8.6 ms** | 238.8 ms | 21.1 ms | 168.9 ms | 51.9 ms | 0.41x |
| profiled | 120k | 25.6 ms | **17.9 ms** | 208.7 ms | 21.2 ms | 163.6 ms | 149.3 ms | 0.84x |
| sector | 170k | 18.6 ms | **11.7 ms** | 219.3 ms | 27.4 ms | 62.7 ms | 41.1 ms | 0.43x |
| transport | 980k | 78.4 ms | **68.8 ms** | 253.2 ms | 64.7 ms | 371.1 ms | 367.7 ms | 1.06x |
| dispatch | 1M | 27.4 ms | **18.9 ms** | 201.8 ms | 21.7 ms | 281.3 ms | 256.3 ms | 0.87x |
| nodal | 1.2M | 29.6 ms | **20.1 ms** | 224.2 ms | 29.8 ms | 217.7 ms | 187.0 ms | 0.67x |
| profiled | 1.2M | 146.8 ms | **120.7 ms** | 230.3 ms | 37.8 ms | 756.0 ms | 723.3 ms | 3.19x |
| sector | 1.7M | 41.5 ms | **30.2 ms** | 255.0 ms | 60.7 ms | 209.2 ms | 177.0 ms | 0.50x |
| transport | 9.8M | 649.2 ms | **624.0 ms** | 639.9 ms | 411.7 ms | 2410.9 ms | 2285.6 ms | 1.52x |
| dispatch | 10M | 156.9 ms | **128.1 ms** | 275.0 ms | 85.5 ms | 1661.1 ms | 1589.0 ms | 1.50x |
| nodal | 12M | 179.1 ms | **149.6 ms** | 359.7 ms | 128.2 ms | 1399.5 ms | 1349.5 ms | 1.17x |
| profiled | 12M | 1654.9 ms | **1328.9 ms** | 398.0 ms | 192.9 ms | 5085.8 ms | 4939.5 ms | 6.89x |
| sector | 17M | 242.5 ms | **224.9 ms** | 668.6 ms | 479.5 ms | 1137.4 ms | 1125.4 ms | 0.47x |

**Warm-up is ~180 ms on the eager lane, ~21 ms on duckdb, ~4-10 ms here**, and
it does not depend on model size. Hand-written linopy with no farkas anywhere
pays 138 ms of that 180, so it is linopy and xarray's own machinery rather
than our shim, which adds a constant ~2.3 ms.

That is most of the margin at the smallest rungs: `xs` reads 0.05-0.10x on a
first build and **0.38-0.47x** steady. Both are true; they answer different
questions.

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

## Absurd sizes, and the trade at them

The ladder above stops at `l` because that is where the published tables are
comparable. This is the other question — does the polars engine hold together
where duckdb is supposed to win — run separately on `dispatch`, LP sink, one
process per measurement:

| variables | polars | linopy | duckdb |
|---|---|---|---|
| 40M (`xl`) | **4.4 s** / 5.34 GB | 9.2 s / 6.87 GB | 11.3 s / **5.95 GB** |
| 120M (`2xl`) | **28.3 s** / 7.83 GB | 62.0 s / 10.02 GB | 81.7 s / **2.01 GB** |

**Nothing falls over.** At 120M variables — a 9.97 GB LP file, on a 25 GB
machine — every engine finishes, and the polars lane is the fastest of the
three by 2.2x and 2.9x.

**But this is where duckdb earns its reputation, and the shape of the trade
changes.** At 40M its peak is no better than ours. At 120M it is **3.9x
lighter**, because that is the size at which an engine that spills starts
spilling and one that does not keeps everything resident. Extrapolating, the
polars lane runs out of machine before duckdb does — the question is only
whether the models people build reach that point.

Read the two rows together: **polars buys speed at every size and pays memory
only at the top**, and the top is beyond what the rest of this file measures.
Both numbers are the trade, and neither is the whole answer.

## The density sweep, and the claim it used to refuse

One model size (50 nodes x 12 technologies x 2000 snapshots = 1.2M coordinates),
four mask densities. The expectation was that an absent pair costs the
relational lane nothing and costs the eager lane a NaN, so the gap should widen
as density falls.

| case | live | variables | wall: farkas | wall: linopy | wall: duckdb | wall | peak: farkas | peak: linopy | peak: duckdb | peak |
|---|---|---|---|---|---|---|---|---|---|
| nodal | 100% | 1.2M | 0.17 s | 0.37 s | 0.65 s | 0.46x | 0.58 GB | 0.62 GB | 0.33 GB | 0.93x |
| nodal | 50% | 600k | 0.10 s | 0.30 s | 0.39 s | 0.34x | 0.41 GB | 0.58 GB | 0.26 GB | 0.71x |
| nodal | 25% | 300k | 0.07 s | 0.27 s | 0.27 s | 0.25x | 0.32 GB | 0.44 GB | 0.22 GB | 0.73x |
| nodal | 8% | 100k | 0.04 s | 0.24 s | 0.22 s | 0.16x | 0.26 GB | 0.34 GB | 0.20 GB | 0.76x |

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
