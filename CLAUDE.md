# CLAUDE.md

## Project Overview

`farkas` is a YAML-based math definition layer for LP/MILP. It lets users define
optimisation problems declaratively in YAML and build them at runtime — natively on the
relational engine (polars → solver or LP file), which is the product path and needs no
[linopy](https://github.com/PyPSA/linopy); or through the opt-in `farkas.linopy` shim,
which puts the same math onto a `linopy.Model` that already exists in memory. Both lanes
accept exactly the same language; there is no routing and no fallback.

Four docs, kept short on purpose — **reference pages carry rules, design notes carry arguments**. If a change makes one longer, check whether it belongs in another:

- `docs/SPEC.md` — the language reference: what a YAML file may contain and what it means. It opens with **§0, the ten laws**; every section below elaborates one.
- `docs/ARCHITECTURE.md` — how it fits together, the hard rules, the module map. Update it in any PR that changes structure.
- `docs/design/ceiling.md` — what may enter the language: the two tiers, the admissibility test, why sink capability is a second axis.
- `docs/ROADMAP.md` — what we build toward and what we have decided never to build.

A PR that adds, renames, or retires a construct updates `docs/SPEC.md` — **§0 if it changes a law, the section if it changes a detail**. Rationale belongs in the PR description or a code comment, not in a new doc section; historical "this used to work differently" notes belong in git.

`docs/models/index.md` is the evidence page — what the language can say and how we know the answers are right. Both its tables are generated (`uv run python -m tools.constructs`), the reference table straight from `examples/ports/references.json`, so a published optimum cannot disagree with an asserted one.

Everything under `docs/` is also published as an mkdocs-material site (`mkdocs.yml`), built from those same files. Two rules: **a new page under `docs/` needs a `nav:` entry** or `mkdocs build --strict` fails in CI, and **a link to anything outside `docs/` is written as a full GitHub URL**, never as `../CONTRIBUTING.md` — the relative form resolves in the repo and 404s on the site, silently. Links inside `docs/` stay relative. `tests/test_docs_site.py` enforces both; see *the docs* in [CONTRIBUTING.md](CONTRIBUTING.md).

**Before proposing a new language feature**, triage it: **macro, primitive, or escape?** Most requests are compositions (macro, free); a genuinely new shape earns a primitive only if it clears the expressive ceiling in `docs/design/ceiling.md` (degree 1 ∩ relational ∩ local); unsayable math goes to a declared `escape:` island (#38) rather than into the language. Check the deliberate non-primitives in `docs/ROADMAP.md` first — parity with another tool is not by itself a reason to add anything.

## Common Commands

Setup, the test loop, what each CI gate means, how to add a port and how to
refresh the benchmarks are in [CONTRIBUTING.md](CONTRIBUTING.md). The essentials:


```bash
# Install (uv-managed venv; [linopy] extra = linopy/xarray for the shim + oracle)
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

# Docs site (own group; `serve` live-reloads, `build --strict` is the CI gate)
uv sync --group docs && uv run mkdocs serve
```

## Package Structure

`docs/ARCHITECTURE.md` carries the authoritative module map, and every module's
own docstring says what it is for. Both are checked —
`tests/test_architecture.py` fails on a module that neither documents — so this
file keeps no third copy to go stale.

## API

```python
import farkas as fk

# No lifetime to manage: the model is frames this process owns, so `sol` stays
# readable as long as it is alive. `close()` and `with` release a large one
# early and nothing breaks without them.
sol = fk.solve('model.yaml', {'p_max': 'p_max.parquet', 'load': 'load.parquet'})
sol.objective
sol.primal('p')  # a polars.DataFrame; .to_pandas / .to_dataarray are the bridges out

# fk.build(...) hands back the live executor, for driving several sinks off one build.
```

Linopy lane — YAML math on a `linopy.Model` that already exists in memory
(requires the `[linopy]` extra):

```python
from farkas import linopy as farkas_linopy

m = farkas_linopy.build('model.yaml', data={...})  # YAML -> linopy.Model
farkas_linopy.extend(m, 'ramp_constraint.yaml', data={...})  # YAML math onto an existing model
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
- Keep the dependency footprint minimal. The runtime set is polars, numpy, pyparsing,
  pydantic, pyyaml, highspy — and *no dataframe library beyond polars*: pandas and
  xarray are bridges *out* (`to_pandas`, `to_dataarray`), shipped with the `[linopy]`
  extra. The bare-install CI job proves the engine builds, solves and reads results
  back without them, and re-resolves at `--resolution lowest-direct` so the declared
  lower bounds stay real rather than decorative. Raise a floor when you rely on a
  version's behaviour; do not raise it to whatever is current.
- Releasing: the git tag *is* the version (hatch-vcs derives it at build time) — never
  hardcode one in `pyproject.toml`. Conventional commits drive the changelog. See `RELEASING.md`.
