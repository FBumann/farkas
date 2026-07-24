# Architecture

Brief, current, precise. If a PR changes the structure described here, it
updates this file in the same PR. Details live in [SPEC.md](SPEC.md);
measured results live in `scratch/relational_spike/README.md`.

## Thesis

A YAML math spec is a **closed AST known before any data is touched**. That
one property makes everything else legal: the whole model can be compiled —
to eager xarray/linopy calls, or to a logical plan executed relationally
under a fixed memory budget — with both paths provably meaning the same
thing. Every architectural rule below exists to protect that property.

## Pipeline

```mermaid
flowchart TB
    Y[YAML file] -->|"parse + validate<br/>(schema.py, validation.py)"| MS[MathSchema]
    MS -->|"expand macros: / expressions: (expansion.py)<br/>expand piecewise: blocks (piecewise.py)<br/>— backends never see any of them"| AST["core AST<br/>= the only contract between layers"]
    AST -->|"api.py: build / solve / write_lp"| LOWER
    AST -.->|"import linopy_yaml.compat<br/>(opt-in: Model.from_yaml, .yaml.extend)"| BUILD
    LOWER -->|"outside the language:<br/>RelationalBuildError naming the construct"| ERR["load error<br/>(no fallback)"]

    subgraph REL["Relational lane — streaming · memory-bounded · linopy-free"]
        direction TB
        LOWER["lowering.py"] --> IR["IR<br/>(relational/ir.py)"]
        DR[("data<br/>parquet paths / pandas")] --> EXEC
        IR --> EXEC["executor.py<br/>tidy tables in file-backed duckdb<br/>under memory_limit"]
        EXEC --> LPS["lp_file sink<br/>portability, debugging<br/>(mps planned)"]
        EXEC --> DIRECT["solver_direct sink<br/>COO batches → highspy → HiGHS"]
        DIRECT --> SOL["solution tables<br/>(label join, never dense)"]
    end

    subgraph EAGER["Compat/oracle lane — opt-in via linopy_yaml.compat · the ONLY lane with linopy · not a runtime dependency"]
        direction TB
        DE[("data<br/>parquet paths / pandas")] --> LOAD["loader.py<br/>coerce data → xr.Dataset"]
        LOAD --> BUILD["builder.py<br/>evaluate AST"]
        BUILD --> MODEL[linopy.Model] --> SOLVE["linopy solve / writers"]
    end

    classDef laneR fill:#f0f7f0,stroke:#3a7d44,stroke-width:2px,color:#111
    classDef laneE fill:#eef1fb,stroke:#4a5fc1,stroke-width:2px,color:#111
    class REL laneR
    class EAGER laneE
```

Eligibility is decided by *attempting the lowering*, so it can never drift
from what the backend actually supports.

## Hard rules

*(Enforced, not aspirational: `tests/test_architecture.py` encodes these rules
as static checks, and CI's bare-install job proves the dependency claims.)*

1. **Core AST is the whole language.** Both backends consume only core AST.
   Anything above it (named expressions, macros) is expanded away before
   dispatch; anything below it (IR, SQL, xarray) is backend-private.
2. **The relational lane is linopy-free.** `linopy_yaml/relational/` imports
   neither the eager builder nor linopy itself — the lane goes duckdb →
   highspy → solver with linopy's semantics as a spec to match, not code to
   share. Engine-internal naming encodes neither "duckdb" nor "yaml".
3. **linopy is kept for exactly two roles: compatibility and validation —
   never runtime.** The streaming engine is the only product lane; constructs
   outside its language are load errors naming the construct, not fallbacks.
   The eager builder lives behind the opt-in `[oracle]` extra
   (`import linopy_yaml.compat`) as the compatibility surface for
   Python-built linopy models and as the correctness oracle. Differential tests (same YAML + data through both
   backends, matching solves) guard every language feature on both.
4. **The full model never resides in this process's memory** on the
   relational path — not as dense arrays, not as a full CSR. Build under a
   duckdb `memory_limit`, hand off in batches, read back by label join. The
   solver's internal copy is the only irreducible full-model residency.
5. **Backend-visible YAML files are self-contained.** No Python-side state
   (registries, session objects) may change what a YAML file means.
6. **The public interface is YAML, full stop.** Python surface is limited to
   the runner (`api.py`: bind sources, build, solve, write) — there is no
   Python modeling API. The IR is internal; a stable IR-construction API is
   a possible later addition, not a current contract.

## The two-tier language

The language is a deliberate economy of **taxed primitives** and **free
composition**:

- **Primitives** (operators, `sum`, `group_sum`, `roll`/`shift`, `where` predicates)
  set the expressive ceiling. Each new primitive costs the full two-backend
  tax: eager implementation, IR node, executor implementation, lowering
  case, differential tests, SPEC entry.
- **Macros and named expressions** (`macros:` / `expressions:` blocks in the
  YAML) are pure AST substitution — every composition of primitives, at zero
  marginal cost and zero divergence risk, because neither backend ever sees
  them.

```mermaid
flowchart TB
    T2["<b>Tier 2 — free composition</b><br/>macros: · expressions:<br/>(edit YAML, nothing else)"]
    T1["<b>Tier 1 — taxed primitives</b><br/>operators · sum · group_sum · roll/shift · where · piecewise:<br/>(the expressive ceiling)"]
    BOTH["both backends, one meaning"]
    ESC["@register Python helpers"]

    T2 -->|"AST substitution before dispatch:<br/>zero marginal cost, zero divergence risk"| T1
    T1 -->|"per new primitive: eager impl + IR node (+ locality class)<br/>+ executor + lowering + differential tests"| BOTH
    ESC -.->|"escape hatch —<br/>router falls back with that reason"| EO["eager backend only"]

    classDef free fill:#f0f7f0,stroke:#3a7d44,stroke-width:2px,color:#111
    classDef taxed fill:#eef1fb,stroke:#4a5fc1,stroke-width:2px,color:#111
    classDef esc fill:#fdf3e7,stroke:#b7791f,color:#111,stroke-dasharray: 4 3
    class T2 free
    class T1 taxed
    class ESC esc
    class EO esc
```

Policy that keeps the economy healthy:

- For any feature request, first ask: **macro or primitive?** Most requests
  are compositions. Only a genuinely new shape (aggregation, coupling)
  earns a primitive.
- **New primitives must be macro-friendly**: anything a user might
  parameterise goes in a *value* position (like `over=`/`into=`), never in
  a kwarg key (the `roll`/`shift` `(x, snapshot=1)` dim-as-key design is
  the counterexample — macros cannot parameterise the dimension).
- **New primitives declare coordinate locality** for the relational
  executor: *pointwise* (joins, masks, group_sum) and *bounded-halo*
  (roll/shift: t±k) compose under partition-wise execution; *global* operators
  (running sums, normalisations) are rejected at lowering with a rewrite
  hint (e.g. running sum → state-variable recurrence).

Deliberately impossible, at every tier: conditionals, iteration, or any
data-dependent structure inside expressions. The declarative substitutes
are `where` masks and `foreach` dims; computation belongs in data prep,
producing a parameter. `@register` Python helpers remain as an explicitly
**eager-only** escape hatch.

## Composition (component libraries)

A component library is a fixed set of parametrised templates agreeing on a
port/flow convention, merged into one program, wired through a data
connectivity table and a single `group_sum` balance. The governing principle:

**Topology is data, not structure.** Wiring a specific system is rows in a
connectivity table, never generated YAML. A well-designed library has
structure bounded by the number of component *types* and cardinality entirely
in data — exactly the shape the streaming engine wants (`foreach` → GROUP BY,
connectivity table → JOIN, balance → aggregate). The library boundary and
the streaming backend are one discipline, not two.

Rules that follow:

- **Compose-then-build.** Schema merge is a pure step producing one
  `MathSchema` before a single lower/stream pass — never incremental build
  per template (`.yaml.extend` is compat-lane sugar; native merge: #30).
- **Namespacing** (qualified names on import) is the missing primitive: #29.
  The port/flow surface (`flow`, connectivity dims) is deliberately shared,
  not namespaced — it is the coupling contract between templates.
- Signs and bidirectional flows need bounds-as-expressions: #31.
- Whatever genuinely is not data (variable port counts, runtime-unknown
  component types) lives in a thin programmatic layer that emits **more rows
  or more templates — never per-instance YAML**. Keep that residue small and
  streaming stays intact.

## Module map

| Module | Role |
|---|---|
| `_patch.py`, `accessor.py` | entry point: `Model.from_yaml()` / `.yaml` accessor |
| `schema.py` | pydantic schema incl. `expressions:` / `macros:` / `piecewise:` blocks |
| `expression_parser.py`, `where_parser.py` | text → core AST |
| `expansion.py` | named-expression / macro substitution (pre-dispatch) |
| `validation.py` | load-time: parse, expand, name-check everything |
| `piecewise.py` | `piecewise:` → λ-formulation declarations (schema-level expansion) + data-time curvature guard |
| `api.py` | native entry point: `check` / `build` / `solve` / `write`, linopy-free |
| `compat.py` | opt-in linopy patching (`[oracle]` extra) |
| `loader.py` | compat/oracle lane: data coercion to xr.Dataset, master coords |
| `builder.py` | eager backend: core AST → linopy.Model |
| `lowering.py` | core AST → IR (defines the relational subset) |
| `relational/ir.py` | frozen logical-plan dataclasses |
| `relational/executor.py` | duckdb execution + lp_file / solver_direct sinks |
| `helpers.py` | built-in + `@register` helpers (eager evaluation) |

## Extension checklists

**Add a macro / named expression:** edit YAML. Nothing else.

**Add a primitive:** grammar (usually free — `f(x, k=v)` already parses) →
eager helper → IR node (+ locality class) → executor → lowering case →
differential test on both sinks → SPEC §5/§7 + this file if structural.
