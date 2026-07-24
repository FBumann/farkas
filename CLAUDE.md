# CLAUDE.md

## Project Overview

`linopy_yaml` is a YAML-based math definition layer for [linopy](https://github.com/PyPSA/linopy).
It lets users define optimisation problems declaratively in YAML and build them into `linopy.Model` objects at runtime.

See `ARCHITECTURE.md` for the architecture (brief, kept current — update it in any PR that changes structure), `SPEC.md` for the full design specification, and `ROADMAP.md` for what we build toward and what we have decided never to build.

**Before proposing a new language feature**, triage it: **macro, primitive, or escape?** Most requests are compositions (macro, free); a genuinely new shape earns a primitive only if it clears the expressive ceiling in `ARCHITECTURE.md` (degree 1 ∩ relational ∩ local); unsayable math goes to a declared `escape:` island (#38) rather than into the language. Check the deliberate non-primitives in `ROADMAP.md` first — parity with another tool is not by itself a reason to add anything.

## Common Commands

```bash
# Install (uv-managed venv; [compat] extra = linopy/xarray for the shim + oracle)
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

See `ARCHITECTURE.md` for the authoritative module map. In brief:

```
linopy_yaml/
├── api.py               # native entry point: check / build / solve / write — linopy-free
├── schema.py            # pydantic schema (incl. expressions:, macros:, piecewise:)
├── expression_parser.py # pyparsing grammar for math expressions
├── where_parser.py      # pyparsing grammar for where strings
├── expansion.py         # macro / named-expression substitution (pre-dispatch)
├── validation.py        # load-time: parse, expand, name-check everything
├── piecewise.py         # piecewise: → λ-formulation declarations
├── helpers.py           # built-in helpers — a CLOSED set, no registry
├── lowering.py          # core AST → IR (defines the streaming subset)
├── relational/          # ir.py + executor.py (duckdb; linopy-free)
├── compat.py            # opt-in linopy patching ([compat] extra)
├── compat.py, loader.py, builder.py                # compat/oracle lane (opt-in)
```

## API

```python
import linopy_yaml as ly

sol = ly.solve("model.yaml", sources={"p_max": "p_max.parquet", ...})
sol.objective
sol.primal("p")
```

Compat lane — YAML math on a `linopy.Model` that already exists in memory
(requires the `[compat]` extra):

```python
from linopy import Model
from linopy_yaml import compat

m = compat.build("model.yaml", data={...})          # YAML -> linopy.Model
compat.extend(m, "ramp_constraint.yaml", data={...})  # YAML math onto an existing model
```

## Development Guidelines

- This package is a **pure consumer** of linopy's public API. Never depend on linopy internals.
- All validation should happen at load time with clear, actionable error messages.
- Use `ruff` for linting/formatting, `mypy` for type checking, `pytest` for tests.
- Keep the dependency footprint minimal.
- Releasing: the git tag *is* the version (hatch-vcs derives it at build time) — never
  hardcode one in `pyproject.toml`. Conventional commits drive the changelog. See `RELEASING.md`.
