# linopy-yaml

Declarative optimisation: define the math in YAML, supply data at runtime, solve.

Models build on a **relational/streaming engine** — tidy tables in duckdb under
a hard `memory_limit`, streamed straight to the solver, so build peak memory
tracks that budget rather than the size of the model.
[linopy](https://github.com/PyPSA/linopy) is not a
runtime dependency; it is kept as an opt-in **compat shim** (YAML math onto a
`linopy.Model` already built in Python) and as the **oracle** every language
feature is differentially tested against. There is no routing and no fallback:
both lanes accept exactly the same language, and a construct outside it is a
load error naming its rewrite.

```mermaid
flowchart LR
    Y["YAML + data"] --> AST["core AST"]
    AST --> R{"inside the<br/>language?"}
    R -->|"yes"| S["streaming engine<br/>duckdb · fixed memory_limit"]
    S --> OUT["solver (batched) / LP file"]
    R -->|"no"| ERR["load error<br/>naming the construct + rewrite"]
    AST -.->|"opt-in shim: same language,<br/>for models already in memory"| E["linopy_yaml.compat"]
    E --> LS["linopy.Model → solve"]

    classDef stream fill:#f0f7f0,stroke:#3a7d44,stroke-width:2px,color:#111
    classDef compat fill:#eef1fb,stroke:#4a5fc1,stroke-width:2px,color:#111
    class S,OUT stream
    class E,LS compat
    class ERR err
    classDef err fill:#fdf3e7,stroke:#b7791f,color:#111
```

## Example

```yaml
# dispatch.yaml
dimensions:
  snapshot: {dtype: int}
  generator: {values: [wind, solar, gas]}
parameters:
  p_max: {dims: [generator]}
  load:  {dims: [snapshot]}
  cost:  {dims: [generator]}
variables:
  p:
    foreach: [snapshot, generator]
    where: "p_max > 0"
    bounds: {lower: 0, upper: p_max}
constraints:
  power_balance:
    foreach: [snapshot]
    equations:
      - expression: sum(p, over=generator) == load
objectives:
  total_cost:
    sense: minimize
    equations:
      - expression: p * cost
```

```python
import linopy_yaml as ly, pandas as pd

sources = {
    "p_max": pd.Series({"wind": 100.0, "solar": 60.0, "gas": 200.0}),
    "cost":  pd.Series({"wind": 1.0, "solar": 2.0, "gas": 50.0}),
    "load":  pd.Series([80, 120, 150, 180, 140, 100],
                       index=pd.RangeIndex(6, name="snapshot")),
}

# the model lives in duckdb, so the Solution owns the executor that backs it
with ly.solve("dispatch.yaml", sources,
              coords={"snapshot": pd.RangeIndex(6, name="snapshot")},
              memory_limit="512MB") as sol:
    print(sol.objective)              # 1920.0
    print(sol.primal("p"))            # tidy pyarrow.Table (snapshot, generator, value)
    print(sol.primal("p").to_pandas())
```

Sources can be anything Arrow-shaped — pyarrow, polars, pandas — or parquet
paths; results come back as `pyarrow.Table`. No dataframe library is a
dependency, so converting is the receiving library's own one-liner.

## Why

- **Declarative math** — readable without knowing the implementation, and
  self-contained: no Python state changes what a file means. It diffs cleanly in
  review and travels as a research artefact.
- **Memory as a config knob** — build peak RAM is set by `memory_limit`, not by
  model size; a 107-million-variable dispatch model builds in ~0.6 GB
  ([benchmarks](docs/benchmarks.md)).
- **Fail early, fail loud** — everything validates at load time, with errors
  naming the problem and the fix.
- **A finite language with a priced way out** — the ceiling is a closure
  (affine ∩ relational ∩ local), not a feature race; genuinely unsayable math
  goes in an `escape:` island, visible in the file and billed before it runs.

The second use case is bolting YAML math onto a Python-built model. Packages
like PyPSA let users modify their core math through callbacks — maximally
flexible, but the modification is invisible in the results, the math hides
inside wiring code, and a Python function is not a sharable artefact. When the
modification *is just math*, a file fixes all three:

```python
compat.extend(m, "ramp.yaml", data={"ramp_max": network.generators["ramp_max"]})
```
```yaml
# ramp.yaml — `p` comes from the model; dims are declared here but their
# coordinates are inferred from it, so no `values:` is needed
dimensions: {snapshot: {dtype: int}, generator: {}}
parameters: {ramp_max: {dims: [generator]}}
constraints:
  ramp_up:
    foreach: [snapshot, generator]
    where: "snapshot > 0 AND ramp_max"
    equations:
      - expression: p - roll(p, snapshot=1) <= ramp_max
```

## Docs

[SPEC.md](SPEC.md) is the language reference · [ARCHITECTURE.md](ARCHITECTURE.md)
for how it fits together · [ROADMAP.md](ROADMAP.md) for what is planned and what
is refused.

To see it rather than read it, `python examples/walkthrough.py` runs one small model through every stage — YAML → schema → core AST → IR → duckdb tables → LP text → solution — printing the artifact each stage produces, plus two models the language refuses and why. Its output is committed as [examples/walkthrough.out](examples/walkthrough.out) if you would rather just read that.

```bash
pip install linopy-yaml            # the streaming engine (duckdb, highspy)
pip install "linopy-yaml[compat]"  # adds linopy + xarray for the shim and oracle
```

Not a solver wrapper, not a domain package, not a data-loading layer — bring
Arrow-compatible tables (pyarrow, polars, pandas) or parquet paths. MIT licensed.

## Status

Alpha, pre-1.0. **Breaking changes land without a deprecation cycle.** When a
construct is named wrong, a default is wrong, or a permissive input turns out
to hide a silent wrong answer, it gets fixed rather than aliased — carrying a
compatibility shim for every earlier spelling would defeat the point of a small
language.

In practice: pin an exact version if you depend on this, and read the
[CHANGELOG](CHANGELOG.md) before upgrading — breaking commits are marked `!`,
and every one names the rewrite. What exists is tested; both lanes round-trip
real models through solve, differentially verified against linopy. It is the
*surface* that is not yet frozen, not the behaviour.
