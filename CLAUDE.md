# CLAUDE.md

## Project Overview

`linopy_yaml` is a YAML-based math definition layer for [linopy](https://github.com/PyPSA/linopy).
It lets users define optimisation problems declaratively in YAML and build them into `linopy.Model` objects at runtime.

See `SPEC.md` for the full design specification.

## Common Commands

```bash
# Install in dev mode
pip install -e .[dev]

# Run tests
pytest

# Lint and format
ruff check .
ruff check --fix .
ruff format .

# Type check
mypy linopy_yaml
```

## Package Structure

```
linopy_yaml/
├── __init__.py          # Public API: exports Model, MathSchema, register
├── model.py             # Model subclass (TEMP — see below), from_yaml classmethod
├── accessor.py          # YamlAccessor class (state + extend())
├── schema.py            # Pydantic models for YAML validation
├── loader.py            # Data coercion, validation, master coords
├── expression_parser.py # pyparsing grammar for math expressions
├── where_parser.py      # pyparsing grammar for where strings
├── builder.py           # Schema + data → linopy Model construction
└── helpers.py           # Built-in helpers (sum, roll) + registry
tests/
├── conftest.py          # Shared fixtures
├── test_schema.py       # YAML schema validation tests
├── test_loader.py       # Data loading and coercion tests
├── test_parser.py       # Expression and where-string parser tests
├── test_accessor.py     # YamlAccessor + extend() behavior
└── test_dispatch.py     # Integration test with the dispatch example
```

## API

> **Note:** SPEC.md describes the target monkey-patch design (`from linopy import Model; import linopy_yaml`). The package currently ships a `linopy_yaml.Model` subclass as a temporary workaround because `linopy.Model.__slots__` does not include `__weakref__`. Once upstream linopy adds it (see `linopy_weakref_issue.md`), `linopy_yaml/model.py` can be removed and the package should switch to the spec's monkey-patch + WeakKeyDictionary design.

```python
from linopy_yaml import Model  # TEMP: subclass; spec target is monkey-patched linopy.Model

# Build from YAML
m = Model.from_yaml("model.yaml", data={...}, coords={...})

# Accessor on YAML-built models
m.yaml.schema    # MathSchema (parsed YAML)
m.yaml.dataset   # xr.Dataset (loaded parameters)
m.yaml.coords    # dict[str, pd.Index] (master coordinates)
m.yaml.extend(...)  # extend with another YAML file
```

## Development Guidelines

- This package is a **pure consumer** of linopy's public API. Never depend on linopy internals.
- All validation should happen at load time with clear, actionable error messages.
- Use `ruff` for linting/formatting, `mypy` for type checking, `pytest` for tests.
- Keep the dependency footprint minimal.
