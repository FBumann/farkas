# linopy-yaml

YAML-based math definition layer for [linopy](https://github.com/PyPSA/linopy).

Define optimisation problems declaratively in YAML, supply data at runtime, and get a standard `linopy.Model` ready to solve.

## Goals

- **Declarative math** — problems are defined in YAML, not Python. Readable without knowing the implementation.
- **Clean boundary** — YAML owns the math definition; Python owns data loading and solving.
- **Pure consumer of linopy's public API** — no internals, no wrapping, no lock-in. The result of `from_yaml()` is a standard `linopy.Model`.
- **Fail early, fail loud** — all validation happens at load time, with error messages that name the problem and suggest the fix.

## Use cases

### 1. Author a full model in YAML

Small-to-medium models, teaching contexts, policy studies, reproducible research. The YAML carries the math; runtime data comes from pandas/xarray.

```python
from linopy_yaml import Model
m = Model.from_yaml("dispatch.yaml", data={...}, coords={...})
m.solve()
```

### 2. Add custom constraints to a Python-built model

The primary use case this package optimises for. Packages like PyPSA, capacity-expansion frameworks, and dispatch models build their core math in Python for good reasons: full linopy feature access, performance control, and complexity that doesn't map cleanly to YAML.

But their users regularly need to add custom constraints — a policy requirement, a pilot technology, a sensitivity scenario. Today the options are unappealing: fork the package, monkey-patch, or maintain a separate Python script that reaches into model internals. A short YAML file is cleaner:

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

The constraint is self-documenting, diffs cleanly, and can be shared or versioned independently from the core model.

### 3. Share and version-control model math as text

YAML files diff cleanly in code review. Colleagues without Python optimisation experience can read and critique the math. Research artefacts travel as files, not as code snippets buried in notebooks.

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

> **Note:** `linopy_yaml` currently ships a `Model` subclass of `linopy.Model` as a temporary workaround — `linopy.Model.__slots__` does not include `__weakref__`, which blocks the monkey-patch design described in [SPEC.md](SPEC.md). Once upstream linopy adds `__weakref__`, the package will switch to the spec's design and you will be able to import `Model` directly from `linopy` again.

```python
from linopy_yaml import Model
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
pip install linopy-yaml
```

Or for development:

```bash
git clone https://github.com/FBumann/linopy-yaml.git
cd linopy-yaml
pip install -e ".[dev]"
```

## YAML Schema

A YAML file has five top-level sections:

| Section        | Purpose                              |
|----------------|--------------------------------------|
| `dimensions`   | Master coordinate definitions        |
| `parameters`   | Named input data with declared shapes|
| `variables`    | Decision variables                   |
| `constraints`  | Linear constraints                   |
| `objectives`   | Objective function(s)                |

See [SPEC.md](SPEC.md) for the full design specification.

## Key Features

- **Pydantic validation** — YAML structure is validated at load time with clear error messages.
- **Expression parser** — pyparsing-based parser for math expressions (`p * cost`, `sum(p, over=generator)`).
- **Where strings** — boolean masks to selectively create variables and constraints (`"p_max > 0"`).
- **Built-in helpers** — `sum(expr, over=dim)` and `roll(array, dim=n)` for aggregation and time-coupling.
- **Custom helpers** — register your own with `@linopy_yaml.register("name")`.
- **Composable models** — use `model.extend("extra.yaml", data={...})` to build models from multiple YAML files.
- **Introspection** — access `model.math` (parsed schema) and `model.dataset` (loaded parameters).

## Status

**v0.0.1** — early prototype. The core pipeline works (schema → loader → parser → builder → linopy.Model). See [SPEC.md](SPEC.md) for the full design and open questions.

## License

MIT
