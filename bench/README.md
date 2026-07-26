# The performance harness

Not shipped in the wheel, not imported by `farkas`, not run in CI. It exists so
that [docs/benchmarks.md](../docs/benchmarks.md) has a *provenance* — the last
set of published numbers came from a `scratch/` script that was deleted, and a
claim nobody can re-run is a claim with a shelf life.

```bash
uv run python -m bench.run                                  # the committed ladder
uv run python -m bench.run --cases dispatch --sizes m l     # one case, two rungs
uv run python -m bench.run --memory-limits 256MB 1GB 4GB    # sweep the budget
uv run python -m bench.report bench/results/latest.jsonl    # -> markdown
```

## What it measures

**Peak RSS and wall time**, per phase, for the same model built two ways:

- `farkas` — `fk.build(...)` then `ex.write_lp(...)`, at a declared
  `memory_limit`.
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
alternative is wrong: memray's tracker slows duckdb ~8x and overcounts polars'
reserved arenas, so it can attribute memory but must never time anything.

**The parity gate runs before any timing.** The smallest rung of each case is
solved on both arms and the objectives compared to 1e-9 relative; a mismatch
aborts the whole run. The differential test suite proves the two lanes agree on
the *language* — it says nothing about the data this harness generates, and a
performance number describing two different models is worse than none.

**Import time is a phase, not a hidden cost.** It is recorded separately and
excluded from `wall_seconds`, because it is fixed and would flatter whichever
engine is measured at small sizes. At the `xs` rung, linopy's import alone
exceeds farkas's entire build.

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
| `sparse` | the same math behind a 2-D `where` | the mask itself: row absence against NaN-padding. `dispatch`'s `where: p_max > 0` removes *nothing* — the generated p_max is always positive — so without this case the ladder only ever measured a dense coord product |
| `transport` | three `group_sum` joins per row | the mapping-table path, where the eager lane must materialise a bus x generator product |

`sparse` also earns its keep on our side of the fence: its mask reads the
leading dim, which puts the label frame on the general sorted path rather than
the arithmetic one, so the ladder covers both.

All three scale on snapshots; `bench/cases.py` holds the ladders. Data is generated
deterministically (a blake2b digest of the shape seeds the RNG — `hash()` is
salted per process and would give the two arms different numbers), cached under
`bench/.cache/`, and feasible by construction: every bus can serve its own load
with no flow at all, so a solve never fails for a reason the harness invented.

Storage (`roll`, the bounded-halo self-join), a MILP through `solver_direct`,
and a `where`-sparsity sweep are the next rungs — see docs/benchmarks.md.

## Adding a case

Add a YAML file under `bench/models/`, a data generator and a ladder to
`CASES` in `bench/cases.py`, and a function turning the same parquet paths into
the linopy lane's `data=`/`coords=` shapes. Nothing else: the runner, the gate
and the report are case-agnostic.
