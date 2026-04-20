# linopy-yaml

YAML-based math definition layer for [linopy](https://github.com/PyPSA/linopy).

Define optimisation problems declaratively in YAML, supply data at runtime, and get a standard `linopy.Model` ready to solve.

## Why?

- **Readable** — YAML math definitions are understandable without knowing Python.
- **Shareable** — version-control, diff, and review optimisation models as text files.
- **Separation of concerns** — the math definition is separate from data loading and solving.

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
