# linopy-yaml

YAML-based math definition layer for [linopy](https://github.com/PyPSA/linopy).

Define optimisation problems declaratively in YAML, supply data at runtime, and solve — through one of two backends: the **eager** backend builds a regular `linopy.Model`, and the **relational** backend streams the same model through duckdb under a fixed memory budget, for models too big to build densely.

## Goals

- **Declarative math** — problems are defined in YAML, not Python. Readable without knowing the implementation.
- **Clean boundary** — YAML owns the math definition; Python owns data loading and solving.
- **Pure consumer of linopy's public API** on the eager path — no internals, no wrapping, no lock-in. The result of `from_yaml()` is a `linopy.Model`. (The relational path matches linopy's semantics without sharing its code — see below.)
- **Fail early, fail loud** — all validation happens at load time, with error messages that name the problem and suggest the fix.

## Use cases

### 1. Author a full model in YAML

Small-to-medium models, teaching contexts, policy studies, reproducible research. The YAML carries the math; runtime data comes from pandas/xarray.

```python
from linopy import Model
import linopy_yaml  # registers .from_yaml and .yaml on linopy.Model

m = Model.from_yaml("dispatch.yaml", data={...}, coords={...})
m.solve()
```

### 2. Add custom constraints to a Python-built model

The primary use case this package optimises for. Packages like PyPSA, capacity-expansion frameworks, and dispatch models build their core math in Python for good reasons: full linopy feature access, performance control, and complexity that doesn't map cleanly to YAML.

Their users still need to modify the model at runtime — a policy requirement, a pilot technology, a sensitivity scenario. The standard answer today is a **callback**: PyPSA's `extra_functionality`, for example, accepts a Python function that runs after the core model is built and adds whatever it wants. That gives you a clean entry point and, because the callback is arbitrary Python, it is also the most flexible option available — anything you can compute, you can use to shape the constraint.

Where callbacks fall short is everything *around* the math:

- **Silent from a results perspective.** PyPSA-style packages treat named components and parameters as the model's own documentation. A callback that calls `model.add_constraints(...)` doesn't show up there — six months later, when you re-read the run, the modification is invisible unless you also go read the Python.
- **Math hidden inside wiring code.** A callback constructs the constraint in Python — index alignment, `.loc[]` lookups, knowing how the host package mapped its components onto linopy variables. The YAML expresses the same constraint as the inequality itself (`p - roll(p, snapshot=1) <= ramp_max`), using only names the model already exposes. The reader sees the math, not the machinery that produced it.
- **Not a sharable artefact.** A callback is a Python function — it lives inside a notebook, a helper module, or a config-loader script. It does not diff cleanly on its own, and it cannot be handed to a colleague without the surrounding code.

A YAML file is strictly less powerful — it can only express math. But when the modification *is just math*, which covers most policy requirements, pilot technologies, and sensitivity scenarios, the YAML addresses all three problems above: it sits next to the parameters and named entities of the model, stays in the user's working vocabulary, and is a self-contained text artefact that travels independently. If your modification needs arbitrary Python in the loop, stay with the callback.

```python
# user adds a custom ramp constraint on top of an existing model
m.yaml.extend("ramp_constraint.yaml", data={"ramp_max": network.generators["ramp_max"]})
```

```yaml
# ramp_constraint.yaml
parameters:
  ramp_max:
    dims: [generator]
constraints:
  ramp_up:
    foreach: [snapshot, generator]
    where: "snapshot > 0 AND ramp_max"
    equations:
      - expression: p - roll(p, snapshot=1) <= ramp_max
```

### 3. Share and version-control model math as text

YAML files diff cleanly in code review. Colleagues without Python optimisation experience can read and critique the math. Research artefacts travel as files, not as code snippets buried in notebooks.

### 4. Build models that don't fit in memory

The same YAML runs on the **relational backend**: expressions become tidy tables in a file-backed duckdb database under a hard `memory_limit`, and the model streams straight to the solver (batched HiGHS calls) or to an LP file — the full model never exists in process memory. Peak build RAM becomes a configuration knob instead of scaling with model size: a 107-million-variable dispatch model builds in ~0.6 GB.

Backend selection is automatic: if the YAML lowers to the relational subset, it streams; anything outside the subset falls back to the feature-complete eager builder with a stated reason. Every language feature is differentially tested — same YAML, same data, both backends, matching solves.

See [ARCHITECTURE.md](ARCHITECTURE.md) for how the two lanes fit together.

## Non-goals

- **Not a solver wrapper** — linopy does that.
- **Not a domain package** — no energy, transport, or any other domain assumptions. This is a general-purpose layer over linopy's API.
- **Not a data loading layer** — users bring their own pandas/xarray objects. No CSV/Parquet/NetCDF readers.

## Open design questions

Decisions the project has not yet finalised. Input welcome — see the linked issues for context.

### What `.yaml` covers

The `.yaml` accessor currently describes only the **YAML-managed portion** of a model, not the whole model. A Python-built model extended with `m.yaml.extend(...)` has a `.yaml` covering the extension, not the Python additions.

Whether to pursue a **complete** `.yaml` representation — intercepting `add_variables()` / `add_constraints()` so `.yaml` always matches the full model — is an open investigation. See [issue #3](https://github.com/FBumann/linopy-yaml/issues/3) for the trade-offs (functional vs readable round-trip) and please weigh in.

## Quick Example

**`dispatch.yaml`:**

```yaml
dimensions:
  snapshot:
    dtype: int
  generator:
    values: [wind, solar, gas]

parameters:
  p_max:
    dims: [generator]
  load:
    dims: [snapshot]
  cost:
    dims: [generator]

variables:
  p:
    foreach: [snapshot, generator]
    where: "p_max > 0"
    bounds:
      lower: 0
      upper: p_max

constraints:
  power_balance:
    foreach: [snapshot]
    equations:
      - expression: sum(p, over=generator) == load

objectives:
  total_cost:
    sense: minimize
    equations:
      - expression: sum(p * cost, over=generator)
```

**Python:**

```python
from linopy import Model
import linopy_yaml  # registers .from_yaml and .yaml on linopy.Model
import pandas as pd

m = Model.from_yaml(
    "dispatch.yaml",
    data={
        "p_max": pd.Series({"wind": 100, "solar": 60, "gas": 200}),
        "load":  pd.Series([80, 120, 150, 180, 140, 100], name="snapshot"),
        "cost":  pd.Series({"wind": 0, "solar": 0, "gas": 50}),
    },
    coords={
        "snapshot": pd.RangeIndex(6, name="snapshot"),
    },
)

m.solve()
print(m.solution["p"])

# Inspect the YAML definition
m.yaml.schema      # parsed MathSchema
m.yaml.dataset     # xr.Dataset of loaded parameters
m.yaml.coords      # master coordinate dict
```

## Installation

```bash
pip install linopy-yaml                # eager backend
pip install "linopy-yaml[relational]"  # + streaming backend (duckdb, highspy)
```

Or for development:

```bash
git clone https://github.com/FBumann/linopy-yaml.git
cd linopy-yaml
pip install -e ".[dev]"
```

## YAML Schema

A YAML file has five top-level sections:

| Section        | Purpose                                                  |
|----------------|----------------------------------------------------------|
| `dimensions`   | Master coordinate definitions                            |
| `parameters`   | Named input data with declared shapes                    |
| `variables`    | Decision variables (incl. binary/integer)                |
| `constraints`  | Linear constraints                                       |
| `objectives`   | Objective function(s)                                    |
| `expressions`  | Named sub-expressions, spliced in by name                |
| `macros`       | Parameterised expression templates (language, not code)  |
| `piecewise`    | Piecewise-linear relations (λ-formulation)               |

See [ARCHITECTURE.md](ARCHITECTURE.md) for the architecture and [SPEC.md](SPEC.md) for the full design specification.

## Key Features

- **Pydantic validation** — YAML structure is validated at load time with clear error messages.
- **Expression parser** — pyparsing-based parser for math expressions (`p * cost`, `sum(p, over=generator)`).
- **Where strings** — boolean masks to selectively create variables and constraints (`"p_max > 0"`).
- **Built-in helpers** — `sum(expr, over=dim)`, `group_sum(expr, mapping, into=dim)` for network topologies, and `roll`/`shift` for time-coupling.
- **Named expressions and macros** — reusable math defined in the YAML itself; expanded before either backend runs, so files stay self-contained.
- **Two backends, one meaning** — automatic routing between the eager linopy builder and the memory-bounded relational/streaming backend, guarded by differential tests.
- **Custom helpers** — register your own with `@linopy_yaml.register("name")` (eager backend only; prefer `macros:` for anything expressible as a composition).
- **Composable models** — use `m.yaml.extend("extra.yaml", data={...})` to build models from multiple YAML files.
- **Introspection** — access `m.yaml.schema` (parsed schema) and `m.yaml.dataset` (loaded parameters).

## Status

**v0.0.2** — early but moving fast. Both backends round-trip real models through solve with differentially verified results; the language covers foreach/where/arithmetic, `sum`/`group_sum`/`roll`/`shift`, named expressions, macros, binary/integer variables, and `piecewise:` blocks. See [ARCHITECTURE.md](ARCHITECTURE.md) and [SPEC.md](SPEC.md).

## License

MIT
