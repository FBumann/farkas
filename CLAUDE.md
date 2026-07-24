# CLAUDE.md

## Project Overview

`linopy_yaml` is declarative optimisation: models are defined in YAML and built on a relational/streaming engine (duckdb under a hard `memory_limit`, streamed to the solver). [linopy](https://github.com/PyPSA/linopy) is not a runtime dependency — it lives behind the `[oracle]` extra as the opt-in compatibility layer (`import linopy_yaml.compat`) and the differential-test oracle.

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

ARCHITECTURE.md's module map is the authoritative list (a test enforces its
completeness). The shape: a shared language front end (schema, parsers,
expansion, validation), the native runner (`api.py`), the streaming engine
(`relational/` — IR + duckdb executor, import-isolated), and the opt-in
linopy compat/oracle lane (`compat.py`, `_patch.py`, `accessor.py`,
`loader.py`, `builder.py`).

## API

The product interface is YAML + the runner (no Python modeling API):

```python
import linopy_yaml as ly

sol = ly.solve('model.yaml', sources={'p_max': 'p_max.parquet', ...},
               coords={'snapshot': range(8760)}, memory_limit='2GB')
sol.objective
sol.primal('p')          # tidy DataFrame (coords..., value)

ly.write_lp('model.yaml', sources, 'model.lp')   # LP-file sink
```

Legacy linopy layer (requires `[oracle]` extra):

```python
import linopy_yaml.compat  # patches linopy.Model
from linopy import Model

m = Model.from_yaml('model.yaml', data={...}, coords={...})
m.yaml.extend('extra.yaml', data={...})
```

## Development Guidelines

- Read ARCHITECTURE.md before structural changes; its hard rules are enforced by `tests/test_architecture.py`.
- The runtime never imports linopy/xarray (CI's bare-install job proves it); the compat lane is a pure consumer of linopy's public API.
- All validation happens at load time with clear, actionable error messages.
- Use `ruff` for linting/formatting (pre-commit wired), `mypy` for type checking, `pytest` for tests; differential tests against the oracle guard every language feature.
- Keep the dependency footprint minimal.
