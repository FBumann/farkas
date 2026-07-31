# Spike: what a duckdb engine would cost now

**Status:** costing exercise, not a proposal. Prices a reversal of `c11a0dd`
(PR #189, 2026-07-28). Nothing here argues for making it — the question is what
it would touch and what it would break, so that a future decision is made
against a number rather than a memory. That is the same reason this harness
exists at all: the decision has already flipped once, and the reasoning for it
did not survive on its own.

**Method:** the tree read module by module for the blast radius; the two engines
measured head to head on the full ladder via the restored `duckdb` foreign arm
([README](README.md#the-duckdb-arm--measuring-an-engine-that-is-not-on-this-branch)).
No engine code was written — the duckdb numbers come from the engine as it
shipped, which is the only version of them worth having.

To re-run everything below:

```bash
git worktree add /tmp/duckdb-arm c11a0dd^ --detach
cd /tmp/duckdb-arm && uv sync
uv run python -m bench.run --arms lpspec duckdb --duckdb-root /tmp/duckdb-arm \
    --sizes xs s m l xl --sinks lp highs --repeat 2 --builds 0 \
    --out bench/results/duckdb-spike.jsonl
```

---

## 1. The seam that survives

`relational/plan.py` (391 lines) is frozen dataclasses with no engine import —
`Expression`, `Predicate`, the five `*Declaration` types, `Program`. It is the
contract, and it is genuinely engine-agnostic: the duckdb engine consumed the
same shapes before #189. Everything above it survives untouched:

| Survives | Lines | Why |
|---|---:|---|
| `language/` (all) | 2,727 | never sees the engine; hard rule 1 |
| `lowering.py` | 382 | AST → plan; touches no data |
| `linopy/` (all) | 1,317 | separate lane, xarray-side |
| `typeset/` (all) | 1,351 | reads the schema, not the plan |
| `relational/plan.py` | 391 | frozen, engine-free |
| `relational/chunking.py` | 44 | pure arithmetic |
| `relational/status.py` | 104 | no polars reference |

That is the good news and it is real: **~6,300 lines never move.** The plan
being the contract is what makes this a swap rather than a rewrite, exactly as
#189 relied on in the other direction.

## 2. The seam that breaks

Everything between the plan and the sink is written in polars expressions.

| Rewritten | Lines | polars refs | Notes |
|---|---:|---:|---|
| `relational/compiler.py` | 717 | 55 | plan → LazyFrame. The bulk of the work |
| `relational/executor.py` | 516 | 43 | build orchestration, `ModelTables` assembly |
| `relational/binding.py` | 269 | 25 | sources → frames; the parquet path |
| `relational/labels.py` | 242 | 10 | algorithm survives, expressions do not |
| `relational/sinks/lp_file.py` | 189 | 38 | LP text emit, float formatting |
| `relational/data_validation.py` | 189 | 18 | one-row-per-coordinate, containment |
| `relational/frames.py` | 108 | 16 | the Arrow recogniser |
| `relational/result.py` | 190 | 6 | `primal`/`dual` joins, `to_*` bridges |
| `relational/sinks/tables.py` | 60 | 5 | contract type changes, logic does not |
| `relational/sinks/highs.py` | 246 | 11 | mostly numpy at the boundary; lightest |

**~2,300 of the engine's 3,317 lines rewritten**, with `highs.py` and
`tables.py` partially spared. Larger than the ~1,500 my note recorded for the
2026-07-27 estimate, because the engine has since grown `chunking.py`,
`data_validation.py`, a split `sinks/` package and a three-strategy `labels.py`.

**The porting surface itself is shallow.** The whole polars API in use is
`.collect`/`.lazy` (41), `.cast` (16), `.over` (11), `.with_row_index` (6),
`.when` (5), `.concat_str` (5), `.shift` (4), `.fill_null` (3),
`.scan_parquet` (2), `.is_in` (2), and one each of `.struct`, `.sink_parquet`,
`.sink_csv`, `.searchsorted`. Every one has a direct SQL counterpart —
`.over` → window function, `.with_row_index` → `ROW_NUMBER`, `.shift` → `LAG`.
Nothing exotic is stranded. The cost is volume and re-derivation, not a
capability gap.

## 3. What has to be re-derived, not translated

Four things where a line-by-line port is the wrong instinct:

- **Absence propagation** (`compiler._propagate_absence`, SPEC §6). A masked
  variable takes its row with it rather than contributing zero. This is the
  semantics that measured 25.0 against 125.0 when linopy's `legacy` default got
  it wrong — `linopy/__init__.py` sets v1 globally because of it. Any new engine
  re-earns this against the differential oracle.
- **Translate/shift edges** (`_translate_fragment`, `_vacated`, `_filled_edge`,
  SPEC §7). Vacated positions drop; filled edges do not. Subtle enough that the
  two lanes still carry a known disagreement — `test_resolution_parity.py`'s
  xfail on orphaned constraint rows.
- **Labels** (`labels.py`). Three strategies — arithmetic, factored, counted —
  chosen by how much of the coordinate product survives the mask, and *they must
  agree with each other*. The concept is portable and in fact duckdb-shaped
  already: my 2026-07-27 note records that writing the label as arithmetic
  instead of a window was what made duckdb competitive there (0.44 s / 0.14 GB),
  because global windows do not spill. This is the one place the port would
  inherit a solved problem.
- **Stable output** (#109). `sinks/README.md` records the hazard: a parallel
  join returns a group in whatever order it finished, so a sink that gathers a
  row's terms then orders rows has already lost order within one. duckdb
  parallelises the same way. The fix ports (carry a sort key, sort once), but
  the property needs re-proving.

## 4. Outside `src/`

- **Tests.** 109 polars references across 15 files. `test_compiler.py` (355
  lines) is the concentrated cost — it asserts against `PolarsCompiler.explain()`,
  which is ARCHITECTURE's admissibility test in executable form; under duckdb it
  becomes reading a SQL string, which is what it was before #189.
  `test_relational.py` (1,080) is mostly behavioural and survives; the
  differential oracle against the linopy lane is engine-blind by construction
  and is the thing that makes the port checkable at all.
- **Bench.** 4 files reference polars; `bench/_run_case.py`'s lpspec arm is
  engine-blind already (it passes parquet paths). `docs/benchmarks.md` and
  `benchmarks-scaling.html` are wholly re-measured — every published number is
  engine-specific.
- **Docs.** ARCHITECTURE (8 refs, incl. hard rule 2's text and the `.explain()`
  admissibility test), benchmarks (7), guide (2), SPEC (2), index (1),
  ROADMAP (1). Plus `pyproject.toml`'s description and keywords, which #189
  rewrote specifically to stop claiming "at any scale" and "streaming".
- **Dependencies.** `polars` out; `duckdb` and `pyarrow` back in. This reverses
  CLAUDE.md's "no dataframe library beyond polars" and changes what the
  bare-install CI job proves. Net footprint grows: duckdb's wheel is larger than
  polars', and pyarrow returns as a hard runtime dep rather than a `[linopy]`
  extra.

## 5. The one user-visible break

`primal()` returns a `polars.DataFrame` — documented in SPEC §10, in
CLAUDE.md's API block, and in `docs/guide.md`. Under duckdb it becomes an Arrow
table or a duckdb relation. `to_pandas` / `to_dataarray` / `to_parquet` are
unaffected (they are bridges either way), and Arrow-in is unaffected (the
recogniser takes any PyCapsule exporter). But anyone doing
`sol.primal('p').filter(...)` in polars idiom breaks. Pre-1.0 and alpha, so
cheap — but it is the one thing that is not internal.

## 6. A prerequisite worth doing regardless

**Hard rule 2 says "Engine-internal naming encodes neither 'polars' nor
'yaml'". `PolarsCompiler` and `PolarsExecutor` violate it in 44 places, and
`tests/test_architecture.py` does not enforce that clause** — it checks the
import fences and the dependency fences, not the naming one. So the rule is
live in the doc and dead in the code.

Renaming to `Compiler` / `Executor` and adding the missing static check is
worth doing on its own merits, and it happens to shrink any future engine swap.
`lps.build` returns the executor as a public return type, so this is a small
public rename too.

## 7. Measured

Full ladder, both engines, both sinks: `bench/results/duckdb-spike.jsonl`.
Render it with

```bash
uv run python -m bench.report bench/results/duckdb-spike.jsonl --arms lpspec duckdb
```

The gate agrees to 0.0e+00 relative on all six cases, including `fleet` and
`sector`, which the duckdb checkout has never seen — the foreign arm builds
today's models on the old engine and reaches the same optimum bit for bit.
That is what makes the rest of this table a measurement rather than a guess.

<!-- LADDER -->

### The binder's own shape

Separately, both engines on the one operation this spike started from — a
12M-row parquet with four columns the model never names (n=2):

| stage | polars | duckdb |
|---|---|---|
| project + materialise (what `binding.py` does per parameter) | 0.10 s / 750 MB | 0.46 s / 476 MB |
| scan feeds a group-by, nothing materialised | 0.11 s / 368 MB | 0.14 s / 128 MB |

Row two is the interesting one: **5.9× between 750 MB and 128 MB for identical
data**, because nothing becomes a resident table. That gap is not really about
the engine — it is about whether the binder materialises. `binding.py:116`
collects deliberately, and its comment records the measurement: making every
collect lazy cost 29% on a join-heavy model to save the 0.15 GB this one collect
saves alone.

## 8. What would have to be true to justify it

The trade is fixed and known: **slower, lighter, plus a settable ceiling and
models that exceed RAM.** So the case has to come from the ceiling, not the
constant factor. Two conditions — and the third one I originally listed here
turns out to be already answered, in the direction that favours a database.

1. **ROADMAP Track 5's declared ceiling becomes a real requirement** — "build
   this in N GB or fail", for models written once and run on a machine chosen by
   someone else.
2. **On the write path specifically.** Track 5 already records that the solver
   is roughly an order of magnitude above the build at 10⁷ variables, so a build
   ceiling is worth having for `lps.write` → LP/MPS handed elsewhere, and worth
   much less when the same process goes on to solve.

### 8a. Partitioning on polars was already measured, and it floors

`f5519b2` (2026-07-27, "PROTOTYPE — partition-wise assembly, measured") answers
this and corrects Track 5's framing in two ways:

- **Emission is the wrong stage.** Emit is 20% of peak on `dispatch` and 2% on
  `transport` (`bench/emit_peak.py`). The memory is in *assembly*, which
  partitions cleanly because `row` is the leading key of the terminal aggregate.
- **It works, and then it stops.** At the `l` rung: `dispatch` 2,213 → 1,710 MiB
  (6.4 → 4.4 s), `transport` 2,657 → 1,695 MiB (9.1 → 9.2 s). 23–36% off peak,
  wall-neutral or better.
- **But peak floors at ~1.7 GB independent of block size**, against duckdb's
  0.88 / 1.16 GB, because the matrix stops being the binding term and everything
  else is still resident: `cols`, the label frames, `rows`, `obj`, the
  parameters. In the commit's own words — *"bounding the build means
  partitioning all of them, which is a buffer manager in application code."*

So the escape hatch I proposed does not exist. Partitioning on polars buys a
constant factor and then hits a floor set by the number of resident frames, and
the remaining distance is a buffer manager. **That is the argument for a
database, and it is already measured.**

### 8b. duckdb mostly does *not* need hand-managed partitioning — and needs less than it did

My 2026-07-24 note ("global windows and ordered aggregates don't spill in
duckdb") was over-broad even then: `8484bb0` corrected it three days later to
*exactly two* forced sites — the global ordered `ROW_NUMBER` for label
assignment, and the LP-text `string_agg`. The plain numeric `GROUP BY` spilled
single-shot at a 256 MB cap.

Re-measured on duckdb 1.5.5, 35M rows, `memory_limit='256MB'`, each operator in
its own process, `COPY … TO parquet` so the optimizer cannot prune it:

| operator | engine role | result |
|---|---|---|
| `GROUP BY` | A-assembly | 5.1 s, **460 MB** peak, 35M rows out |
| `ROW_NUMBER() OVER (ORDER BY …)` | label assignment | 6.0 s, **512 MB** peak, 31.5M rows out |
| `string_agg(… ORDER BY …)` | LP-text sink | **OutOfMemoryException** at 244 MB |

**The label window now spills — that changed since July.** One of the two forced
sites is gone by upgrade. The other is the ordered `string_agg`, which still
OOMs exactly as recorded, but it is in the debugging sink and is avoidable: the
current polars `lp_file` already emits a frame of lines carrying its own sort
key and sorts once (`sinks/README.md`, "Stable output"), which is a shape that
does not need an ordered aggregate at all.

Caveats on this measurement: synthetic shapes, not the engine's real queries,
and the `sort_only` arm was confounded by input that was already in sort order,
so it is omitted from the table. The two rows that matter each wrote their full
output to disk (192 MB and 131 MB of parquet), which is what rules out the
optimizer having skipped the work.

## 9. Estimate

| | |
|---|---|
| engine rewritten | ~2,300 lines of 3,317 |
| survives untouched | ~6,300 lines above the plan |
| test churn | ~500 lines, concentrated in `test_compiler.py` |
| docs + bench | full re-measure of `benchmarks.md` + the scaling page; ~20 doc refs |
| public break | `primal()` return type |
| prerequisite | `Polars*` rename (44 sites) + the missing hard-rule-2 check |

Comparable to #189 in the other direction and somewhat larger, against a
measured 1.7–5.2× wall-clock regression.

**Where this leaves it.** The cost above is the honest price, and it has not
moved. What moved is the counter-argument: partitioning polars to a bound was
measured and floors at ~1.7 GB (§8a), and the hand-managed partitioning duckdb
used to need has largely evaporated (§8b). So the choice is no longer "swap the
engine or build the partitioning" — it is **buy a bounded build for a 1.7–5.2×
wall-clock regression, or do not have one.** That is a product question about
whether Track 5's ceiling is a real requirement, not an engineering question
about whether it is reachable.

**Recommended next step, if this is live:** re-measure #189's six-model ladder
with `lps.write` as the sink rather than `solve`, since §8's condition 2 says
the write path is the only place the ceiling pays. #189's headline compares to a
loaded solver, where the build is ~9% of peak and the regression is diluted. The
write path is where duckdb would look its best and polars its worst, and nobody
has published that column.
