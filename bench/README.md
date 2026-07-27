# The performance harness

Not shipped in the wheel, not imported by `farkas`, not run in CI. It exists so
that [docs/benchmarks.md](../docs/benchmarks.md) has a *provenance* — the last
set of published numbers came from a `scratch/` script that was deleted, and a
claim nobody can re-run is a claim with a shelf life.

```bash
uv run python -m bench.run                                  # the committed ladder
uv run python -m bench.run --cases dispatch --sizes m l     # one case, two rungs
uv run python -m bench.report bench/results/latest.jsonl    # -> markdown
```

## What it measures

**Peak RSS and wall time**, per phase, for the same model built two ways:

- `farkas` — `fk.build(...)` then `ex.write_lp(...)`.
- `linopy` — `farkas.linopy.build(...)` then `Model.to_file(io_api='lp-polars')`.

Both arms read the same parquet files and produce an LP file, so the comparison
is one language, one output format, two engines. The linopy arm is the right
comparison and the only one worth making first: it accepts *exactly* the same
YAML (ARCHITECTURE.md hard rule 3), which is what makes it the oracle rather
than a rival dialect.

Not measured, deliberately: solve time (that is HiGHS, identical either way, and
it would swamp the build), and anything about expressiveness.

## Why it is built this way

**One process per measurement.** Peak RSS is a property of a process. A second
arm in the same interpreter inherits the first's high-water mark and its warm
allocator, so `bench/run.py` never imports farkas or linopy — it only spawns
`bench/_run_case.py` and reads one JSON line back.

**`ru_maxrss`, not a tracker.** The same kernel counter `/usr/bin/time -l`
reports, read via `resource.getrusage`. `docs/benchmarks.md` records why the
alternative is wrong: memray's tracker slows an allocation-heavy engine
several-fold and overcounts reserved arenas, so it can attribute memory but must
never time anything.

**The parity gate runs before any timing.** The smallest rung of each case is
solved on both arms and the objectives compared to 1e-9 relative; a mismatch
aborts the whole run. The differential test suite proves the two lanes agree on
the *language* — it says nothing about the data this harness generates, and a
performance number describing two different models is worse than none.

## Where the clock starts and stops

The easiest way to publish a wrong number is to time something in one arm that
the other never does. The boundaries are therefore explicit:

| | farkas | linopy |
|---|---|---|
| **before the clock** | splitting parquet paths into parameters vs dimensions (harness bookkeeping — it re-parses the YAML only because the *runner* decides which file is which) | — |
| `import` | `import farkas` | `import farkas.linopy` → linopy, xarray |
| `build` | `fk.build(...)` — the engine scans the parquet itself | `read_parquet` + reshape + `farkas.linopy.build(...)` |
| `emit` | `ex.write_lp(path)` | `Model.to_file(path, io_api='lp-polars')` |
| `teardown` | `ex.close()` — releases the built model | — (nothing to release) |
| **after the clock** | row counts, `count(*) FROM A`, scratch-dir size | `nvars` / `ncons` |

Three of those are deliberate calls rather than defaults:

- **Import is excluded from `wall_seconds`** but recorded. It is fixed, paid
  once per process, and at the `xs` rung linopy's import alone exceeds farkas's
  entire build — including it would make the small end meaningless.
- **Teardown is included.** Releasing a scratch database is work a user pays
  for, and the arm that has one should be charged for it.
- **`progress=False` is passed to linopy.** Its default is
  `m._xCounter > 10_000`, so every rung above `xs` would render tqdm bars that
  the farkas arm has no equivalent of — ~7% of the write at 10M variables, and
  stderr noise in a harness that parses stdout.

Both arms start from the same parquet files and end with an LP file on disk, so
each pays for its own data ingestion. That is the honest unit; note that the
*phases* are not comparable one-for-one, because linopy defers coefficient
materialisation to `to_polars()` inside `to_file` — its `build` allocates dense
arrays and little else. Compare totals, and read the phases as attribution
within an arm.

**Memory is not the whole cost.** farkas trades RAM for a scratch database on
disk; `workdir_bytes` records how much, so a peak-RSS win cannot quietly hide a
multi-gigabyte temp file. It is sampled at the end of emit, not at its peak.

**Failures are results.** A run that dies is written to the JSONL with the
exception line that killed it, and the report renders it as a cell. An OOM at a
declared budget is the single most informative thing this harness can find — it
falsifies hard rule 4 for that shape.

**Repeats collapse by minimum.** Noise only ever adds.

## The cases

Chosen so each stresses a *different* SQL shape (ARCHITECTURE.md, "read the
verdict off the SQL"), not to cover the language:

| case | shape | why |
|---|---|---|
| `dispatch` | pointwise bounds + one `sum` per row | raw throughput, and the case a dense eager broadcast is best at — so our worst ratio |
| `nodal` | `(snapshot, node, tech)`, `where: installed > 0` | sparsity as it actually occurs — see below |
| `transport` | three `group_sum` joins per row | the mapping-table path, where the eager lane must materialise a bus x generator product |

**`nodal` is the case worth explaining.** It is dispatch over nodes and
technologies, and a technology only generates at a node where it is installed:
no offshore wind inland, no hydro without a river. PyPSA spells that by
attaching generators to buses, Calliope by declaring techs at nodes; in YAML it
is a `where` over the capacity table. 50 nodes x 12 technologies is 600
coordinates per snapshot, of which 3 per node — a quarter — exist. That gap is
the comparison: relationally an absent pair is an absent row, eagerly it is a
NaN that still costs eight bytes and a broadcast.

The sparsity is *structural and time-invariant*, which is not incidental —
`installed` carries node and tech but not snapshot. A random Bernoulli mask
would sweep the same densities while misrepresenting the shape, and the shape is
what an engine can exploit.

`Shape.density` (technologies per node: 12 / 6 / 3 / 1) is swept at one model
size, because sweeping size and density together leaves no way to tell one
effect from the other. Run the full ladder with `--sizes all`.

**The report measures what survived rather than trusting the declaration.**
`dispatch` declares `where: p_max > 0` against a p_max that is always positive,
so its mask removes nothing and the engine pays for it anyway; the `live` column
says `100%` and makes that visible instead of leaving it as a trap. Keeping that
vacuous mask is itself a measurement, which is why `nodal` is a separate case
rather than a fix to `dispatch`'s data.

Data is generated deterministically (a blake2b digest of the shape seeds the
RNG — `hash()` is salted per process and would give the two arms different
numbers), cached under `bench/.cache/`, and feasible by construction.

Storage (`roll`, the bounded-halo self-join) and a MILP through `solver_direct`
are the next rungs — see docs/benchmarks.md.

## The other harness: regression benchmarks

`bench/regressions/` asks a different question — *did this change make it
worse?* — and answers it with a different metric, deliberately.

```bash
uv sync --group bench
uv run pytest bench/regressions --benchmark-memory
uv run pytest bench/regressions --benchmark-memory-compare=0001 \
    --benchmark-memory-compare-fail=mean:10%
```

It is [`pytest-benchmem`](https://github.com/fluxopt/pytest-benchmem): a memray
peak pass on top of pytest-benchmark's timing, with `isolate=True` so every pass
is a fresh process — a warm allocator would otherwise be measured instead of the
build, and isolation is what makes whole-process `rss` available beside the
memray peak.

**Why memray here and not in the published ladder.** Measured on `dispatch/m`:

| arm | `ru_maxrss` | memray peak |
|---|---|---|
| farkas | 309 MB | 211 MB |
| linopy | 604 MB | **2967 MB** |

memray counts polars' reserved arenas as allocated and does not count the
interpreter or mapped libraries at all, so the bias points in *opposite*
directions in the two lanes: the peak ratio is 0.51x by RSS and 0.07x by memray.
A published cross-library claim built on that would be false the moment a reader
ran `/usr/bin/time`. Within one lane the same bias sits on both sides of a diff
and cancels, leaving a metric that is deterministic and attributable to a call
stack — which RSS, sensitive to machine load, is not.

So: RSS for the comparison we publish, memray for the regressions we chase.
`--benchmark-memory-compare-fail` is what turns the second into a gate.

## Adding a case

Add a YAML file under `bench/models/`, a data generator and a ladder to
`CASES` in `bench/cases.py`, and a function turning the same parquet paths into
the linopy lane's `data=`/`coords=` shapes. Nothing else: the runner, the gate
and the report are case-agnostic.
