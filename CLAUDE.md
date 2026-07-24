# CLAUDE.md

## Project Overview

`linopy_yaml` is a YAML-based math definition layer for [linopy](https://github.com/PyPSA/linopy).
It lets users define optimisation problems declaratively in YAML and build them into `linopy.Model` objects at runtime.

See `ARCHITECTURE.md` for the architecture (brief, kept current — update it in any PR that changes structure) and `SPEC.md` for the full design specification.

## Common Commands

```bash
# Install (uv-managed venv; oracle extra = linopy/xarray for differential tests)
uv sync  # dev group (tools + oracle deps) is default

# Run tests
uv run pytest

# Lint and format
uv run ruff check .
uv run ruff check --fix .
uv run ruff format .

# Type check
uv run mypy linopy_yaml

# Hooks (once per clone)
uv run pre-commit install
```

## Package Structure

```
linopy_yaml/
├── __init__.py          # Applies the monkey-patch on import; exports MathSchema, register, Model alias
├── _patch.py            # Monkey-patches linopy.Model with .from_yaml() and .yaml (WeakKeyDictionary-backed descriptor)
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

Requires linopy>=0.7 (the release that added `__weakref__` to `Model.__slots__`).

```python
from linopy import Model
import linopy_yaml  # registers .from_yaml and .yaml on linopy.Model

# Build from YAML
m = Model.from_yaml("model.yaml", data={...}, coords={...})

# Accessor on any model — lazy on Python-built models
m.yaml.schema    # MathSchema (parsed YAML), or None on Python-built models
m.yaml.dataset   # xr.Dataset of loaded parameters (empty on Python-built models)
m.yaml.coords    # dict[str, pd.Index], inferred from variables when not declared
m.yaml.extend(...)  # extend with another YAML file
```

## Development Guidelines

- This package is a **pure consumer** of linopy's public API. Never depend on linopy internals.
- All validation should happen at load time with clear, actionable error messages.
- Use `ruff` for linting/formatting, `mypy` for type checking, `pytest` for tests.
- Keep the dependency footprint minimal.
