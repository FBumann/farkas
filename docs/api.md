# Python API

How you *run* a model. The model itself is the YAML file —
[SPEC](SPEC.md) is what it may contain and what it means; this page is the
sixteen names that load, check, build, solve and read one back. The surface is
pinned by a test and the reasoning behind its size is
[ARCHITECTURE](ARCHITECTURE.md#the-python-surface).

Five verbs — `check`, `load_schema`, `build`, `solve`, `write` — and the
exception tree rooted at `LinopyYamlError`: `LanguageError` (with `SchemaError`,
`DimensionError`, `PiecewiseExpansionError`) for the model, `DataError` for what
was bound to it.

```python
import lpspec as lps

lps.check('model.yaml')  # parse → validate → lower, no data bound
schema = lps.load_schema('model.yaml')  # MathSchema

result = lps.solve('model.yaml', sources, solver_options={'time_limit': 60})
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
ex.write_lp('model.lp')
result = ex.solve()
```

What `sources` accepts is [SPEC §8](SPEC.md#8-data-binding). Nothing on this
path imports linopy, and `primal` returns a `polars.DataFrame` — Arrow-backed, so it exports the same protocol the
loader recognises. `to_pandas` and `to_dataarray` are the bridges out and need
pandas / xarray, which ship with the `[linopy]` extra. The only build knob is
`coords`; **`solver_options` is not a build knob** and is forwarded verbatim to
the solver.

Reading a result:

| Rule | |
|---|---|
| **`is_ok` is not `has_primal`** | `is_ok` rolls up the termination condition; `has_primal` adds the solver's verdict on whether an incumbent exists, and is what every reader gates on. A MIP that hits `time_limit` before finding a feasible point is `ok` with nothing to read |
| reading anyway | `NoSolutionError`; `objective` is `nan` |
| `dual` **raises rather than zero-filling** | no values at all is `NoSolutionError`; values but no duals — any integer or binary variable makes them undefined — is `LinopyYamlError`, because only this quantity is missing |
| duals exist only on `solver_direct` | a model written to LP and solved elsewhere never passes back through here. Reduced costs and slacks ride the same join and are not exposed yet |
| `to_dataset` costs what it says | each variable arrives dense over its own dims — name a subset, or use `to_parquet` |
| `write` | `.lp` only today; `.mps` raises `NotImplementedError` |

**The linopy shim** (`lpspec.linopy.build` / `.extend`, `[linopy]` extra) puts
the same YAML math on a `linopy.Model` that already exists in memory. It is
documented with everything else about that relationship in
[docs/design/linopy.md](design/linopy.md#3-the-shim).
