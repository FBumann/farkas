# Python API

How you *run* a model. The model itself is the YAML file —
[SPEC](SPEC.md) is what it may contain and what it means; this page is the
nineteen names that load, check, build, solve and read one back. The surface is
pinned by a test and the reasoning behind its size is
[ARCHITECTURE](ARCHITECTURE.md#the-python-surface).

Six verbs — `check`, `load_schema`, `build`, `solve`, `solve_over`, `write` — and the
exception tree rooted at `LpspecError`: `LanguageError` (with `SchemaError`,
`DimensionError`, `PiecewiseExpansionError`) for the model, `DataError` for what
was bound to it.

```python
import lpspec as lps

lps.check('model.yaml')  # parse → validate → lower, no data bound
schema = lps.load_schema('model.yaml')  # MathSchema

result = lps.solve('model.yaml', sources, solver_options={'time_limit': 60})
# ...or solver_name='gurobi', the other solver sink — same model either way
result.status, result.termination_condition, result.objective
result.is_ok  # rolled-up verdict: not an error, abort or refusal
result.has_primal  # narrower: are there values to read
result.primal('p')  # tidy frame (dims…, value) — the native shape
result.dual('power_balance')  # shadow prices, the same shape and the same join
result.to_pandas('p')  # the same, as a DataFrame
result.to_dataarray('p')  # the same, labelled: .sel / resample / plot
result.to_dataset()  # every variable by default; names for a subset
result.to_parquet(directory)  # streamed to disk, never through this process

lps.write('model.yaml', sources, 'model.lp')  # sink chosen by the suffix
```

**Nothing has to be released.** The built model is frames this process owns, so
`primal` and the `to_*` readers stay valid for as long as the `Result` does.
`close()` and the context-manager protocol exist to hand a large model back
early, not because forgetting them breaks anything. `lps.build` returns the
executor when one build should feed more than one sink:

```python
ex = lps.build('model.yaml', sources)
ex.write('model.lp')
result = ex.solve()
```

What `sources` accepts is [SPEC §8](SPEC.md#8-data-binding). Nothing on this
path imports linopy, and `primal` returns a `polars.DataFrame` — Arrow-backed, so it exports the same protocol the
loader recognises. `to_pandas` and `to_dataarray` are the bridges out and need
pandas / xarray, which ship with the `[linopy]` extra. The only build knob is
`coords`; **`solver_options` is not a build knob** and is forwarded verbatim to
the solver.

**Which solver is a caller's choice, not the file's.** `solver_name` is
`highs` (ships with the package) or `gurobi` (needs the `[gurobi]` extra), and
nothing in the YAML names one — the same file means the same model whichever
takes it. Options travel in the chosen solver's own vocabulary,
`{'time_limit': 60}` for HiGHS against `{'TimeLimit': 60}` for Gurobi, because
forwarding verbatim is the contract and translating names would mean holding an
opinion about every option either one has. A name outside the two is an error
listing them, never a quiet fallback to the default.

Reading a result:

| Rule | |
|---|---|
| **`is_ok` is not `has_primal`** | `is_ok` rolls up the termination condition; `has_primal` adds the solver's verdict on whether an incumbent exists, and is what every reader gates on. A MIP that hits `time_limit` before finding a feasible point is `ok` with nothing to read |
| reading anyway | `NoSolutionError`; `objective` is `nan` |
| `dual` **raises rather than zero-filling** | no values at all is `NoSolutionError`; values but no duals — any integer or binary variable makes them undefined — is `LpspecError`, because only this quantity is missing |
| duals exist only where a solver ran | either solver sink hands them back through the same join; a model written to LP and solved elsewhere never passes back through here. Reduced costs and slacks ride that join too and are not exposed yet |
| `to_dataset` costs what it says | each variable arrives dense over its own dims — name a subset, or use `to_parquet` |
| `write` | the **suffix** picks the writer — `.lp` today, `.mps` a `NotImplementedError` naming it as planned, anything else a `ValueError` listing both sets. Checked before the build, so a format nothing can write costs no model |

## Solving one model many times

`solve_over` runs the same model once per slice and folds the answers. It is a
driver over `solve`, not a second engine: **a plan cannot contain a loop; a
process may loop over plans** ([the ceiling](design/ceiling.md)). Scenarios,
rolling horizons and myopic pathways are all the same fold.

```python
runs = lps.solve_over(
    'model.yaml', sources, lps.EachCoordinate('scenario'), keep=('p',), executor=ProcessPoolExecutor(4)
)
runs.objective  # (scenario, status, termination_condition, objective)
runs.primal('p')  # (scenario, snapshot, generator, value)

runs = lps.solve_over(
    'window.yaml',
    sources,
    lps.EachWindow('snapshot', length=48, step=24, into='t'),
    carry={'soc_initial': ('soc', 23)},
    keep=('p', 'soc'),
)
```

| Rule | |
|---|---|
| **a partition is a filter on the sources** | not a narrower `coords` — the containment check refuses parameter rows outside the declared coordinates, by design. The axis rewrites the sources and supplies the matching `coords` together |
| **`keep` is mandatory in practice** | a fold releases each slice's model as it goes, so peak stays at one slice. What is not extracted inside the loop cannot be read afterwards |
| **no aggregate objective** | `objective` is a frame keyed by slice. Scenarios are a distribution, not a sum; summing window objectives double-counts whatever the overlap discards |
| **duals are not exposed** | a window's shadow price is that window's. Concatenating them into a price curve is wrong in a way nothing complains about |
| `carry` is a copy, never arithmetic | `{parameter: (variable, index)}`. Accumulation — `existing += built` — is a derived variable in the YAML, where the math is reviewable |
| the carry index is explicit | with `EachWindow(…, 48, 24, …)` the state to carry is at local index 23, not 47. An implicit "last" is correct until overlap is introduced and silently wrong after |
| **a window spans coordinates, not values** | `length=48` is forty-eight snapshots however they are numbered, so the dimension only has to be **orderable** — datetimes, strings and gapped integers all work. `into` is a dense `0..n-1` local index, which is what keeps the seam's `where: "t == 0"` matching, and it has no default because the name belongs to the model |
| non-positional grouping | *"each calendar month"* has unequal groups, so it is a precomputed column plus `EachCoordinate`. What `EachWindow` uniquely offers is **overlap** |
| `carry` excludes `executor` | a carried value makes slice *i+1* depend on slice *i*, so the slices cannot run concurrently. Refused rather than one silently winning |
| `shared_fs` | whether the executor's workers can read your paths — the one fact the `Executor` protocol cannot report. It only affects sources that do *not* carry the slice key |

### Choosing an executor

`executor` is any [`concurrent.futures.Executor`](https://docs.python.org/3/library/concurrent.futures.html#executor-objects) —
a `submit` returning a `Future`, and nothing else. That is deliberate: **this
package ships no remote transport and no vendor integration**, so the executor
is the only extension point there is, and it has to be one anybody can
implement. `tests/test_strategy.py` runs the whole sweep through a nine-line
class to keep that honest.

| | use it when | notes |
|---|---|---|
| `None` *(default)* | always, until measurement says otherwise | sequential. Nothing is serialised, because nothing crosses a boundary |
| `ThreadPoolExecutor` | rarely | works, and sources are **not** encoded — but polars is already multithreaded, so slices contend with its pool, and threads share an address space so peak is additive rather than per-worker |
| `ProcessPoolExecutor` | genuine local parallelism | **must not use `fork`** — see below. Sources cross as parquet |
| anything remote | a cluster you already run | dask's `Client`, ray's wrappers, loky. Pass `shared_fs=True` only if the workers really do share your filesystem |

**A forked worker hangs.** polars' thread pool does not survive `fork`, and the
failure is a **hang** rather than an error — indistinguishable from a slow
solve, which makes it the worst shape a failure can take. Measured: `fork` never
returns where `spawn` and `forkserver` both do. It cannot be enforced from
inside `solve_over`, because a remote executor has no start method to inspect,
so pass the context yourself:

```python
import multiprocessing
from concurrent.futures import ProcessPoolExecutor


def main():
    ctx = multiprocessing.get_context('spawn')  # or 'forkserver'
    with ProcessPoolExecutor(4, mp_context=ctx) as pool:
        runs = lps.solve_over('model.yaml', sources, lps.EachCoordinate('scenario'), keep=('p',), executor=pool)


if __name__ == '__main__':  # spawn re-imports your module; without this it recurses
    main()
```

**Parallel is N × peak.** Each worker holds its own slice's model, so a
four-way pool wants four times the memory of one slice. That is a machine
decision, and the reason `None` is the default rather than a pool sized for
you.

Sources cross a process boundary as parquet, never as pickled frames: measured
over 1M rows that is 8.3x smaller and 3x faster. A path the workers can reach
stays a path; one they cannot travels as its own bytes untouched, because
decoding and re-encoding a parquet file produces identical output for 79x the
CPU. **Pass paths or frames, whichever you already have** — there is nothing to
tune. (`df.lazy()` is not an optimisation: an eager frame is embedded in the
plan, so it pickles *larger* than the frame. Only `scan_parquet` is a
reference.)

**The linopy shim** (`lpspec.linopy.build` / `.extend`, `[linopy]` extra) puts
the same YAML math on a `linopy.Model` that already exists in memory. It is
documented with everything else about that relationship in
[docs/design/linopy.md](design/linopy.md#3-the-shim).
