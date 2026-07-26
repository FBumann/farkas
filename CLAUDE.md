# CLAUDE.md

## Project Overview

`linopy_yaml` is a YAML-based math definition layer for [linopy](https://github.com/PyPSA/linopy).
It lets users define optimisation problems declaratively in YAML and build them into `linopy.Model` objects at runtime.

Three docs, kept short on purpose — if a change makes one of them longer, check whether it belongs in another:

- `SPEC.md` — the language reference: what a YAML file may contain and what it means.
- `ARCHITECTURE.md` — how it fits together, the hard rules, the expressive ceiling, the module map. Update it in any PR that changes structure.
- `ROADMAP.md` — what we build toward and what we have decided never to build.

A PR that adds, renames, or retires a construct updates `SPEC.md`. Rationale belongs in the PR description or a code comment, not in a new doc section; historical "this used to work differently" notes belong in git.

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
uv run pyrefly check

# Hooks (once per clone)
uv run pre-commit install
```

## Package Structure

See `ARCHITECTURE.md` for the authoritative module map. In brief:

```
src/linopy_yaml/
├── api.py               # native entry point: check / build / solve / write — linopy-free
├── schema.py            # pydantic schema (incl. expressions:, macros:, piecewise:)
├── expression_parser.py # pyparsing grammar for math expressions
├── where_parser.py      # pyparsing grammar for where strings
├── expansion.py         # macro / named-expression substitution (pre-dispatch)
├── validation.py        # load-time: parse, expand, name-check everything
├── piecewise.py         # piecewise: → λ-formulation declarations
├── helpers.py           # built-in helpers — a CLOSED set, no registry
├── lowering.py          # core AST → logical plan (defines the streaming subset)
├── sources.py           # bind runtime data to a validated schema
├── errors.py            # the exception hierarchy (LinopyYamlError root)
├── relational/          # plan.py + compiler.py + executor.py + sinks/ (duckdb; linopy-free)
└── compat/              # opt-in linopy lane ([compat] extra): the ONLY code
                         # importing linopy/xarray — __init__.py, builder.py, loader.py
```

## API

```python
import linopy_yaml as ly

# Solution holds the duckdb executor that backs primal/to_* — use a with block
# (or sol.close()); ly.build(...) returns the live executor for multiple sinks.
with ly.solve("model.yaml", {"p_max": "p_max.parquet", "load": "load.parquet"}) as sol:
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
- Use `ruff` for linting/formatting, `pyrefly` for type checking, `pytest` for tests.
- pyrefly runs on the `strict` preset with zero errors and is gated in CI. Keep it
  that way: fix the type, don't widen it. If a finding is genuinely wrong, suppress
  the one line with `# pyrefly: ignore[rule-name]` and say why — do not turn the rule
  off globally. The rules `pyproject.toml` deliberately leaves unpromoted are
  documented there with the reason.
- Keep the dependency footprint minimal.
- Releasing: the git tag *is* the version (hatch-vcs derives it at build time) — never
  hardcode one in `pyproject.toml`. Conventional commits drive the changelog. See `RELEASING.md`.
