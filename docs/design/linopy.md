# Relationship to linopy

Everything about [linopy](https://github.com/PyPSA/linopy) in one place, because
it is otherwise the kind of thing that gets mentioned everywhere and explained
nowhere. Three separate relationships, and conflating them is what made the rest
of the docs noisy:

| | What | Where it matters |
|---|---|---|
| **Not a dependency** | the product path never imports it | packaging |
| **The oracle** | how we know the answers are right | testing |
| **The shim** | an opt-in way to put YAML math on a `linopy.Model` | a narrow, real use case |

## 1. It is not a runtime dependency

`lps.solve`, `lps.build`, `lps.write` and `lps.check` go YAML → polars → HiGHS
or LP file, and import nothing from linopy, xarray or pandas. CI proves it: the
bare-install job runs the whole suite with none of them present.

`pip install "lpspec[linopy]"` adds linopy, xarray and pandas, which buys two
things and nothing else — the shim below, and the `to_pandas` / `to_dataarray`
bridges out of a result.

**One leak.** The public exception tree is rooted at `LinopyYamlError`, a name
left over from when this package was called `linopy-yaml`. Every traceback on
the pure-polars path names a library that path never loads. Renaming it is
breaking and therefore
[tracked separately](https://github.com/FBumann/lpspec/issues/389).

## 2. It is the oracle

Correctness here is not "the tests pass"; it is **the same YAML, built both
ways, produces the same model**. The differential suite builds a model through
the relational engine and through linopy, and compares.

That is only meaningful because both paths consume the *same resolved AST* and
neither may hold its own opinion about what a name means — the narrow waist in
[ARCHITECTURE](../ARCHITECTURE.md#one-contract-many-consumers). If they resolved
names independently, the suite would be comparing two dialects rather than
checking one language.

It also has a known blind spot, which is why the model gallery exists: a
**shared misreading** passes the differential suite green. Only an outside
published optimum catches that, and
[docs/models/index.md](../models/index.md) is where those live.

**Where a concept is already linopy's, we copy its name** — solve statuses,
`status` / `termination_condition` as two axes with `is_ok` as the rollup, the
shape of a result. Our audience arrives from linopy and PyPSA, and a second
vocabulary for one fact is a tax on all of them. But **copy it, do not import
it**: the engine may not import linopy, so the tables live here and a test
imports linopy to assert the copy still matches. A copy nobody checks is a copy
that rots.

## 3. The shim

For math that belongs on a `linopy.Model` **already in memory** — a PyPSA
network, say, where the model is built by something else and you want to add
declared constraints to it.

```python
from lpspec import linopy as lpspec_linopy

m = lpspec_linopy.build('model.yaml', data={...}, coords={...})  # -> linopy.Model
lpspec_linopy.extend(m, 'ramp.yaml', data={...})  # mutates m in place
```

Both are *pure producers*: YAML in, model out, nothing retained. `build` returns
a plain `linopy.Model` — no accessor, no attached schema, no patched attributes
— so nothing is lost across `pickle`, `deepcopy` or `to_netcdf`. To inspect the
math, re-read the file with `lps.load_schema`.

`extend` may reference variables already on the model (they come from the model
argument, not from Python-side history), while the YAML must still declare every
parameter *and dimension* it uses — the declaration is required, the `values:`
are not, since they can come from the model. Coords precedence for `extend`: the
`coords=` kwarg, then coords inferred from the model's variables, then `values:`
in the YAML, then error. A `values:` contradicting the model's existing
coordinate is an error, not a silent override.

### The same language, different data inputs

The shim accepts **exactly the same language** — that equality is what makes the
oracle an oracle, and a construct outside the language is a load error naming
the rewrite, never a redirection to the other path.

What differs is what each will take as *data*, which is a wart rather than a
design ([#60](https://github.com/FBumann/lpspec/issues/60)):

| | product path (`sources=`) | shim (`data=` / `coords=`) |
|---|---|---|
| dimension labels | `sources`, then `coords=`, then `values:`, then **derived from the parameter tables** | `coords=`, then `values:`, then error — no derivation |
| a parameter | parquet path, or any table exporting the Arrow PyCapsule protocol; `int`/`float` for 0-D | `int`/`float` (broadcasts freely), `dict` / `pd.Series` for 1-D, `pd.DataFrame` for 2-D, `xr.DataArray` directly |
| unnamed index levels | — | bind positionally to the declared dims; named levels bind by name |

The derivation row is the one that bites: on the product path a dimension some
parameter already spans needs no second declaration, but it costs the *declared
order*, which `shift` reads positionally — so pass an explicit index whenever
order matters. A dimension declaring `coords` cannot be derived at all, since
derivation reads index columns only.

## What we deliberately do not take

Array operations (`merge`, `reindex`, `stack`), the Python modeling API, and the
solver layer. The first is data prep
([SPEC §11](../SPEC.md#11-out-of-scope)), the second is
[hard rule 5](../ARCHITECTURE.md#hard-rules) — the model is the file you review
and diff — and the third is
[#106](https://github.com/FBumann/lpspec/issues/106), where we adopt linopy's
*design* for declared solver capabilities without adopting its code.

Where linopy is genuinely ahead, and why none of it is a ceiling question, is the
honest snapshot in [ROADMAP](../ROADMAP.md#honest-snapshot).
