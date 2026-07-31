# Measured results

**Cost is a property of the engine, not of the language.** The rules in
`docs/ARCHITECTURE.md` constrain what a file may say and would survive an engine
swap untouched; what a build *costs* is settled here, by measurement. That
separation is why this file can be rewritten by a benchmark run without
anything in `docs/SPEC.md` moving.

Peak RSS and wall time for the same model built two ways — declaratively on the
relational engine, and eagerly through linopy — from the same parquet files to
the same destination. `wall` and `peak` columns are **lpspec ÷ linopy: below
1.00 is a win for us.** The [chart page](benchmarks-scaling.html) plots the same
run.

**The eager arm is `lpspec.linopy.build`, not hand-written linopy** — our own
YAML→`linopy.Model` shim, so it carries our loader on top of linopy's work.
Against hand-written linopy on the same model the shim costs a constant
**~2.3 ms**: a fixed offset, nowhere near enough to move a conclusion.

**Two sinks, and they are not the same comparison.** The LP file is the artifact
fewest callers want; `highs` is the one most reach for, and there HiGHS's own
dense model is resident in both arms, which narrows every ratio. Read the sink
you actually use.

## How to reproduce it

Read straight off [`latest.jsonl`](https://github.com/FBumann/lpspec/blob/main/bench/results/latest.jsonl) and
[`density.jsonl`](https://github.com/FBumann/lpspec/blob/main/bench/results/density.jsonl), each carrying the machine
fingerprint, the library versions and the commit that produced it. Two files
because a run *replaces* its output: one narrower than the tables it publishes
would leave them unprovenanced while still looking complete.

```bash
uv run python -m bench.run --sizes xs s m l --repeat 3
uv run python -m bench.run --sizes d100 d50 d25 d08 --skip-gate --repeat 3 \
    --out bench/results/density.jsonl
uv run python -m bench.report bench/results/latest.jsonl bench/results/density.jsonl
uv run python -m bench.plot  # refreshes the chart page's numbers
```

**Measure on an idle machine.** An earlier version of these tables was taken
while the laptop was doing other work and it inflated `profiled` by 55% —
enough to turn "level" into "the one case we lose". Best of three, nothing else
running.

macOS-26.2-arm64-arm-64bit-Mach-O, python 3.13.2, 26 GB · lpspec 0.0.1a55 ·
polars 1.43.0 · linopy 0.8.0.post1.dev140+g346943317 (the v1-semantics build,
PyPSA/linopy#717) · highspy 1.15.1 · numpy 2.5.1. Parity gate: all six cases
agree to 0.0e+00 relative (`fleet` to 4.6e-16) before anything is timed.

*This lane replaced a duckdb engine, and the three-way comparison that decided
it — speed against a settable memory ceiling — is in
[#189](https://github.com/FBumann/lpspec/pull/189) and in git. It is not
re-measured here: duckdb is no longer a dependency, and a column nobody can
re-run is a claim with a shelf life.*

## Results

### dispatch — highs sink

Both arms end holding a populated `highspy.Highs` with `run()` never called: lpspec through `build_highs`, linopy through `to_highspy()`. The simplex is the same work whoever filled the model, so timing it would say nothing about the lane that filled it.

| variables | live | rows | wall: lpspec | wall: linopy | wall | peak: lpspec | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 10k | 100% | 100 | 0.02 s | 0.20 s | 0.08x | 0.17 GB | 0.21 GB | 0.83x | — |
| 100k | 100% | 1k | 0.02 s | 0.22 s | 0.10x | 0.20 GB | 0.23 GB | 0.87x | — |
| 1M | 100% | 10k | 0.08 s | 0.36 s | 0.21x | 0.50 GB | 0.50 GB | 0.99x | — |
| 10M | 100% | 100k | 0.58 s | 1.90 s | 0.31x | 2.89 GB | 3.31 GB | 0.87x | — |

### dispatch — lp sink

lpspec writes the LP file, linopy through its `lp-polars` writer.

| variables | live | rows | wall: lpspec | wall: linopy | wall | peak: lpspec | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 10k | 100% | 100 | 0.01 s | 0.21 s | 0.07x | 0.17 GB | 0.22 GB | 0.76x | 1 MB |
| 100k | 100% | 1k | 0.03 s | 0.22 s | 0.12x | 0.21 GB | 0.27 GB | 0.77x | 7 MB |
| 1M | 100% | 10k | 0.12 s | 0.34 s | 0.36x | 0.43 GB | 0.59 GB | 0.73x | 76 MB |
| 10M | 100% | 100k | 1.09 s | 1.64 s | 0.67x | 1.58 GB | 2.20 GB | 0.72x | 796 MB |

### fleet — highs sink

Both arms end holding a populated `highspy.Highs` with `run()` never called: lpspec through `build_highs`, linopy through `to_highspy()`. The simplex is the same work whoever filled the model, so timing it would say nothing about the lane that filled it.

| variables | live | rows | wall: lpspec | wall: linopy | wall | peak: lpspec | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 12k | 100% | 6.02k | 0.04 s | 0.28 s | 0.15x | 0.17 GB | 0.21 GB | 0.84x | — |
| 120k | 100% | 60.2k | 0.05 s | 0.31 s | 0.17x | 0.22 GB | 0.25 GB | 0.89x | — |
| 1.2M | 100% | 602k | 0.15 s | 0.56 s | 0.26x | 0.61 GB | 0.71 GB | 0.86x | — |
| 12M | 100% | 6.02M | 1.12 s | 3.15 s | 0.36x | 4.07 GB | 5.38 GB | 0.76x | — |

### fleet — lp sink

lpspec writes the LP file, linopy through its `lp-polars` writer.

| variables | live | rows | wall: lpspec | wall: linopy | wall | peak: lpspec | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 12k | 100% | 6.02k | 0.04 s | 0.29 s | 0.14x | 0.18 GB | 0.22 GB | 0.79x | 1 MB |
| 120k | 100% | 60.2k | 0.06 s | 0.31 s | 0.18x | 0.24 GB | 0.26 GB | 0.95x | 9 MB |
| 1.2M | 100% | 602k | 0.21 s | 0.45 s | 0.46x | 0.63 GB | 0.46 GB | 1.39x | 89 MB |
| 12M | 100% | 6.02M | 1.79 s | 1.99 s | 0.90x | 2.39 GB | 1.60 GB | 1.49x | 920 MB |

### nodal — highs sink

Both arms end holding a populated `highspy.Highs` with `run()` never called: lpspec through `build_highs`, linopy through `to_highspy()`. The simplex is the same work whoever filled the model, so timing it would say nothing about the lane that filled it.

| variables | live | rows | wall: lpspec | wall: linopy | wall | peak: lpspec | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 3k | 25% | 1k | 0.02 s | 0.21 s | 0.08x | 0.17 GB | 0.21 GB | 0.82x | — |
| 30k | 25% | 10k | 0.02 s | 0.22 s | 0.10x | 0.19 GB | 0.22 GB | 0.86x | — |
| 300k | 25% | 100k | 0.05 s | 0.30 s | 0.15x | 0.33 GB | 0.36 GB | 0.92x | — |
| 3M | 25% | 1M | 0.30 s | 1.08 s | 0.28x | 1.35 GB | 1.78 GB | 0.76x | — |

### nodal — lp sink

lpspec writes the LP file, linopy through its `lp-polars` writer.

| variables | live | rows | wall: lpspec | wall: linopy | wall | peak: lpspec | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 3k | 25% | 1k | 0.02 s | 0.21 s | 0.07x | 0.17 GB | 0.22 GB | 0.76x | 0 MB |
| 30k | 25% | 10k | 0.02 s | 0.22 s | 0.09x | 0.19 GB | 0.25 GB | 0.77x | 2 MB |
| 300k | 25% | 100k | 0.07 s | 0.28 s | 0.25x | 0.32 GB | 0.46 GB | 0.69x | 25 MB |
| 3M | 25% | 1M | 0.52 s | 0.84 s | 0.62x | 1.06 GB | 1.57 GB | 0.68x | 264 MB |

### profiled — highs sink

Both arms end holding a populated `highspy.Highs` with `run()` never called: lpspec through `build_highs`, linopy through `to_highspy()`. The simplex is the same work whoever filled the model, so timing it would say nothing about the lane that filled it.

| variables | live | rows | wall: lpspec | wall: linopy | wall | peak: lpspec | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 12k | 100% | 1k | 0.02 s | 0.21 s | 0.09x | 0.18 GB | 0.21 GB | 0.84x | — |
| 120k | 100% | 10k | 0.03 s | 0.23 s | 0.14x | 0.26 GB | 0.25 GB | 1.04x | — |
| 1.2M | 100% | 100k | 0.19 s | 0.47 s | 0.41x | 0.82 GB | 0.65 GB | 1.27x | — |
| 12M | 100% | 1M | 1.83 s | 2.47 s | 0.74x | 3.91 GB | 4.74 GB | 0.82x | — |

### profiled — lp sink

lpspec writes the LP file, linopy through its `lp-polars` writer.

| variables | live | rows | wall: lpspec | wall: linopy | wall | peak: lpspec | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 12k | 100% | 1k | 0.02 s | 0.21 s | 0.08x | 0.18 GB | 0.23 GB | 0.78x | 1 MB |
| 120k | 100% | 10k | 0.04 s | 0.23 s | 0.17x | 0.26 GB | 0.30 GB | 0.87x | 9 MB |
| 1.2M | 100% | 100k | 0.28 s | 0.42 s | 0.66x | 0.73 GB | 0.69 GB | 1.07x | 95 MB |
| 12M | 100% | 1M | 2.88 s | 2.20 s | 1.31x | 2.44 GB | 3.14 GB | 0.78x | 986 MB |

### sector — highs sink

Both arms end holding a populated `highspy.Highs` with `run()` never called: lpspec through `build_highs`, linopy through `to_highspy()`. The simplex is the same work whoever filled the model, so timing it would say nothing about the lane that filled it.

| variables | live | rows | wall: lpspec | wall: linopy | wall | peak: lpspec | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 1k | 6% | 1k | 0.02 s | 0.22 s | 0.09x | 0.17 GB | 0.21 GB | 0.82x | — |
| 10k | 6% | 10k | 0.02 s | 0.23 s | 0.10x | 0.19 GB | 0.23 GB | 0.83x | — |
| 100k | 6% | 100k | 0.04 s | 0.33 s | 0.14x | 0.31 GB | 0.50 GB | 0.61x | — |
| 1M | 6% | 1M | 0.31 s | 1.48 s | 0.21x | 0.96 GB | 2.97 GB | 0.32x | — |

### sector — lp sink

lpspec writes the LP file, linopy through its `lp-polars` writer.

| variables | live | rows | wall: lpspec | wall: linopy | wall | peak: lpspec | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 1k | 6% | 1k | 0.02 s | 0.22 s | 0.08x | 0.17 GB | 0.22 GB | 0.77x | 0 MB |
| 10k | 6% | 10k | 0.02 s | 0.23 s | 0.09x | 0.19 GB | 0.25 GB | 0.75x | 1 MB |
| 100k | 6% | 100k | 0.05 s | 0.31 s | 0.16x | 0.30 GB | 0.53 GB | 0.58x | 12 MB |
| 1M | 6% | 1M | 0.39 s | 1.28 s | 0.30x | 0.94 GB | 2.91 GB | 0.32x | 120 MB |

### transport — highs sink

Both arms end holding a populated `highspy.Highs` with `run()` never called: lpspec through `build_highs`, linopy through `to_highspy()`. The simplex is the same work whoever filled the model, so timing it would say nothing about the lane that filled it.

| variables | live | rows | wall: lpspec | wall: linopy | wall | peak: lpspec | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 9.8k | 100% | 1.4k | 0.02 s | 0.23 s | 0.10x | 0.18 GB | 0.21 GB | 0.84x | — |
| 98k | 100% | 14k | 0.03 s | 0.25 s | 0.13x | 0.22 GB | 0.24 GB | 0.93x | — |
| 980k | 100% | 140k | 0.12 s | 0.43 s | 0.27x | 0.57 GB | 0.56 GB | 1.02x | — |
| 9.8M | 100% | 1.4M | 1.10 s | 2.48 s | 0.44x | 2.99 GB | 3.90 GB | 0.77x | — |

### transport — lp sink

lpspec writes the LP file, linopy through its `lp-polars` writer.

| variables | live | rows | wall: lpspec | wall: linopy | wall | peak: lpspec | peak: linopy | peak | LP |
|---|---|---|---|---|---|---|---|---|---|
| 9.8k | 100% | 1.4k | 0.02 s | 0.23 s | 0.10x | 0.18 GB | 0.23 GB | 0.78x | 1 MB |
| 98k | 100% | 14k | 0.04 s | 0.24 s | 0.15x | 0.23 GB | 0.29 GB | 0.80x | 8 MB |
| 980k | 100% | 140k | 0.19 s | 0.40 s | 0.47x | 0.54 GB | 0.67 GB | 0.82x | 79 MB |
| 9.8M | 100% | 1.4M | 1.76 s | 2.14 s | 0.82x | 1.90 GB | 1.78 GB | 1.07x | 820 MB |

## What this says

**Ahead on both, on all six models**, at the `l` rung through the sink most
callers use:

| | dispatch | fleet | nodal | profiled | sector | transport |
|---|---|---|---|---|---|---|
| wall | **0.31x** | **0.36x** | **0.28x** | **0.74x** | **0.21x** | **0.44x** |
| peak | **0.87x** | **0.76x** | **0.75x** | **0.82x** | **0.32x** | **0.75x** |

**`profiled` is no longer the case in the ladder we lose**, and it is worth
saying why rather than deleting the sentence that said we did. It was level
through this sink (1.09x) and an unambiguous loss through the LP one; it is now
0.74x and 1.31x. A parameter dense over the whole variable product is the array
xarray already wants and a full-size join for us — the shape has not changed,
the joins got cheaper. It stays in the set for the same reason it was put there.

**The LP file is still the weaker route**, and two numbers are against us there:
`profiled` **1.31x** on wall, and `fleet` **1.47x** on peak with `transport` at
1.07x. What is left is structural in two ways — most of an LP write is turning
doubles into text, work neither lane avoids, so the wall ratio compresses toward
1.00 however fast the build gets; and COO carries a `(row, col, coeff)` triple
per nonzero where a dense array carries one float, which is `fleet`'s 1.47x. The
many-declaration shape is the one that shows it, which is why that case exists.

**Sparsity is what separates the peak numbers**, not size. At the same rung
`sector` is 0.32x of linopy's peak and `dispatch` 0.87x: a coordinate that does
not exist is an absent row here and a NaN in a dense array there, so the gap
tracks how much of the coordinate product a model actually uses.

### Two costs, and both are real

The harness spawns one process per measurement, so every timing above is a
*first* build, which pays whatever lazy work a lane does on its first call.
That is the right number for a caller who builds one model and solves it, and
the wrong one for a rolling horizon, which pays it once.

### Marginal cost per model

Build only, repeated in one process. **first** is what a caller pays who builds one model and solves it; **steady** is what every model after the first costs in a rolling horizon. Every lane does lazy first-call work that a loop never pays again — ~180 ms of it on the eager lane, ~4 ms here.

| case | vars | lpspec: first | lpspec: steady | linopy: first | linopy: steady | steady vs linopy |
|---|---|---|---|---|---|
| transport | 9.8k | 23.0 ms | **15.3 ms** | 262.7 ms | 38.3 ms | 0.40x |
| dispatch | 10k | 11.0 ms | **6.0 ms** | 208.2 ms | 13.9 ms | 0.44x |
| fleet | 12k | 40.5 ms | **33.8 ms** | 308.6 ms | 101.9 ms | 0.33x |
| nodal | 12k | 14.9 ms | **9.1 ms** | 231.0 ms | 18.8 ms | 0.49x |
| profiled | 12k | 14.4 ms | **7.6 ms** | 199.6 ms | 18.5 ms | 0.41x |
| sector | 17k | 13.7 ms | **9.0 ms** | 212.2 ms | 23.6 ms | 0.38x |
| transport | 98k | 29.0 ms | **22.6 ms** | 320.9 ms | 54.3 ms | 0.42x |
| dispatch | 100k | 15.0 ms | **9.1 ms** | 214.9 ms | 14.8 ms | 0.61x |
| profiled | 120k | 25.8 ms | **17.9 ms** | 206.6 ms | 20.0 ms | 0.90x |
| fleet | 120k | 47.1 ms | **38.0 ms** | 305.5 ms | 102.3 ms | 0.37x |
| nodal | 120k | 15.3 ms | **8.8 ms** | 208.8 ms | 19.4 ms | 0.46x |
| sector | 170k | 16.4 ms | **11.2 ms** | 213.0 ms | 28.4 ms | 0.39x |
| transport | 980k | 264.5 ms | **183.2 ms** | 454.7 ms | 110.7 ms | 1.66x |
| dispatch | 1M | 33.8 ms | **25.0 ms** | 217.0 ms | 20.6 ms | 1.22x |
| fleet | 1.2M | 88.0 ms | **73.9 ms** | 314.3 ms | 108.8 ms | 0.68x |
| nodal | 1.2M | 28.9 ms | **19.3 ms** | 222.1 ms | 29.9 ms | 0.65x |
| profiled | 1.2M | 134.6 ms | **117.8 ms** | 225.5 ms | 35.1 ms | 3.35x |
| sector | 1.7M | 35.6 ms | **28.3 ms** | 285.6 ms | 78.2 ms | 0.36x |
| transport | 9.8M | 1596.5 ms | **1675.4 ms** | 966.3 ms | 601.4 ms | 2.79x |
| dispatch | 10M | 190.1 ms | **184.2 ms** | 318.3 ms | 75.8 ms | 2.43x |
| profiled | 12M | 1233.5 ms | **1251.1 ms** | 478.2 ms | 198.6 ms | 6.30x |
| fleet | 12M | 511.0 ms | **489.3 ms** | 437.4 ms | 182.4 ms | 2.68x |
| nodal | 12M | 165.2 ms | **150.6 ms** | 363.9 ms | 140.5 ms | 1.07x |
| sector | 17M | 264.4 ms | **314.3 ms** | 1039.9 ms | 559.4 ms | 0.56x |

## Absurd sizes

`dispatch`, LP sink, six rungs spanning 12,000x — the top two are past anything
the ladder covers. Best of two, read from `bench/results/scaling.jsonl`,
plotted in [benchmarks-scaling.html](benchmarks-scaling.html):

| variables | lpspec | linopy | wall | peak |
|---|---|---|---|---|
| 10k | **0.01 s** / 0.17 GB | 0.20 s / 0.22 GB | **0.07x** | **0.76x** |
| 100k | **0.03 s** / 0.21 GB | 0.22 s / 0.26 GB | **0.12x** | **0.79x** |
| 1M | **0.12 s** / 0.44 GB | 0.33 s / 0.58 GB | **0.37x** | **0.76x** |
| 10M | **1.09 s** / 1.57 GB | 1.65 s / 2.15 GB | **0.66x** | **0.73x** |
| 40M | **4.54 s** / 5.42 GB | 6.91 s / 6.52 GB | **0.66x** | **0.83x** |
| 120M | **18.38 s** / 7.28 GB | 38.65 s / 9.29 GB | **0.48x** | **0.78x** |

**Nothing falls over**, on either lane, at a 9.97 GB LP file. The wall ratio
does not decay with size — 0.48x at 120M against 0.66x at 10M, because linopy's
curve steepens above 10M and this one does not.

**Peak does not run out, and this page used to say it did.** The previous
version of this section read 1.07x at 120M and concluded that "whatever memory
headroom this lane has over the eager one is a small-and-sparse-model property,
not a scaling one". That conclusion does not survive re-measurement: peak is
0.73-0.83x across four orders of magnitude, 0.78x at the top rung.

Two things moved it, and only one of them is us. `cols` became positional
([#433](https://github.com/FBumann/lpspec/pull/433)) and took ~20% off this
case's peak. The rest is that the earlier reading was taken in a run *this
section itself flagged as noisy* — it recorded linopy at 38.5 s and 106.2 s for
the same rung. The honest summary is that the old 1.07x was a conclusion drawn
across the noise floor, and the caveat below was already telling the reader not
to trust it.

*The `2xl` rung is still the noisiest* — 8-10 GB of peak on a 26 GB machine is
where other things start to matter, and it is best-of-two rather than
best-of-three. Read the ordering, not the seconds.

## The density sweep, and the claim it used to refuse

One model size (50 nodes x 12 technologies x 2000 snapshots = 1.2M coordinates),
four mask densities. The expectation was that an absent pair costs the
relational lane nothing and costs the eager lane a NaN, so the gap should widen
as density falls. One model size, through the `lp` sink; `live` is how many of
the 12 technologies each node has installed.

| case | live | variables | wall: lpspec | wall: linopy | wall | peak: lpspec | peak: linopy | peak |
|---|---|---|---|---|---|---|---|---|
| nodal | 100% | 1.2M | 0.17 s | 0.39 s | 0.45x | 0.55 GB | 0.65 GB | 0.84x |
| nodal | 50% | 600k | 0.11 s | 0.31 s | 0.34x | 0.42 GB | 0.61 GB | 0.69x |
| nodal | 25% | 300k | 0.07 s | 0.27 s | 0.25x | 0.32 GB | 0.46 GB | 0.70x |
| nodal | 8% | 100k | 0.04 s | 0.24 s | 0.16x | 0.26 GB | 0.35 GB | 0.74x |

**It now does, and it did not before.** Wall time falls from 0.45x to 0.16x as
density drops, and peak improves at every rung — 0.84x, 0.69x, 0.70x, 0.74x.
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

## Sink capabilities

What each sink can ingest, measured against the shipped solvers rather than
assumed. The architectural reading is in
[docs/design/ceiling.md](design/ceiling.md#capability-is-not-the-ceiling); the plan is
[ROADMAP Track 3](ROADMAP.md#track-3--capabilities-and-the-degree-line).

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
Two of its entries are load-bearing elsewhere — `README.md` and `docs/ROADMAP.md`
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

Recorded in [`bench/README.md`](https://github.com/FBumann/lpspec/blob/main/bench/README.md) — one process per
measurement, `ru_maxrss` rather than a tracker, import excluded from
`wall_seconds` and teardown included, and a parity gate that aborts the run
before anything is timed if the two lanes disagree. Failures are results and are
rendered as cells.

Measurement pitfall worth keeping: memray's tracker slows an allocation-heavy
engine several-fold and overcounts reserved arenas, so it can attribute memory
but must never time anything. Peak RSS is the gate metric; memray is for
attribution only.
