# The ceiling

**What may enter the language, and what may never.** The structure that holds
it is [ARCHITECTURE](../ARCHITECTURE.md); the rules a model must obey are
[SPEC §0](../SPEC.md#0-the-laws); what is planned or refused is
[ROADMAP](../ROADMAP.md). This page is the argument in between: the test a
candidate primitive has to pass, why capability is a second axis rather than
part of the ceiling, and what composition would force.

It is a claim, so it carries evidence — math a ported model needed and this
language could not state becomes a ledger row in
[the gallery](../models/index.md#ledger--what-a-port-could-not-say).

## Two tiers, and the ceiling

**Primitives** (operators, `sum`, `group_sum`, `roll`/`shift`, `where`
predicates) set the expressive ceiling, and each costs the full two-backend tax:
eager implementation, plan node + locality class, executor case, lowering case,
differential tests, SPEC entry. **`macros:` / `expressions:`** are pure AST
substitution — every composition of primitives at zero marginal cost and zero
divergence risk. **Formulations** (`piecewise:`) are taxed like a primitive but
compose like a macro: they emit *new declarations* before dispatch and never
enter as plan expression nodes.

For any request, triage: **macro, primitive, or escape?** Most are compositions.
New primitives must be **macro-friendly** — anything a user might parameterise
goes in a *value* position like `over=`/`by=`, never a kwarg key; the
`shift(x, over=snapshot, by=1)` takes its dimension in a kwarg *value*, so a
macro can pass one as a formal — the dim-as-key design that could not is gone.

A candidate primitive is admissible iff it is all three of **degree 1 (affine)**
— `variable × parameter`, never `variable × variable`, the one axis that is a
scope choice rather than a consequence of streaming
([ROADMAP](../ROADMAP.md#the-degree-axis)); **relational** — filter / join /
group-by-aggregate over tidy tables; and **local** — *pointwise* or
*bounded-halo*, which compose under partition-wise execution where *global*
operators do not. Locality is judged in **data space**: reductions over a
*coordinate* space ("the last snapshot") read only the small, already
materialised dim tables and stay admissible even though they look global.

**Read the verdict off the plan.** Rules 2 and 3 are one question asked twice,
and the compiler already answers it — write the candidate's query over the term
stream first and read `.explain()`:

| Shape of the emitted query | Locality | Rules 2–3 |
|---|---|---|
| filter on a column already in the frame | pointwise | admissible |
| equi-join against a parameter or mapping table | pointwise | admissible |
| join on the dim table at `ord ± k`, `k` fixed | bounded-halo | admissible |
| dim table only, no data join | coordinate-space | admissible (free) |
| window over unbounded rows, or a recursive CTE | global | **reject**, with the rewrite |

This is the case analysis `_sum_fragment`, `_group_fragment` and
`_translate_fragment` already implement — each rewriting one fragment on its
own, which is what *pointwise* and *bounded-halo* mean in code — so a candidate
fitting none of those shapes has no executor to be written into. Two limits: **degree is not a property of the plan**, and it
presumes the terminal `sum(coeff)` over `(row, col)` stays the only aggregate a
*term* passes through.
A primitive is finished when `lowering.py` accepts it and the differential test
against the linopy oracle passes.

**The ceiling is a claim, so it needs evidence.** In
[docs/models/index.md](../models/index.md), math a ported model needed and this language
could not state becomes a ledger row with its triage verdict — what
[docs/ROADMAP.md](../ROADMAP.md) should be argued from. Those ports also cover a class
no other test reaches: both lanes consume the same resolved AST by rule 1, so a
*shared misreading* passes the differential suite green, and only an outside
optimum catches it.

What is *outside* the closure splits three ways, and the split decides whether a
request can ever be met:

| Tier | Bounded by | Members | Can it move? |
|---|---|---|---|
| **Capability-bounded** | what a given sink can ingest | SOS / indicator (#23); quadratic | per sink — see below |
| **Budget-bounded** | the escape *label* budget — a cap on the rows and columns an island may emit | global operators, arbitrary Python, non-relational manipulation | already movable — that is what an island is |
| **Design-bounded** | our choice of where work belongs | data prep, domain helpers, Python declaring structure | movable any time; we don't want to |

Impossible **in the symbolic plan**: conditionals, iteration, any data-dependent
structure inside expressions. What is protected is that the plan's **shape** is
fixed before any data is read — which declarations exist and which dims each
spans. *Cardinality* is always data's to supply: `foreach: [snapshot]` does not
know how many snapshots there are either.

That distinction decides more than it looks. A dimension whose members are
*computed* in data prep is completely ordinary — a cycle basis for
[KVL](../models/pypsa_kvl.md), the subsets of a subtour-elimination
family — because a graph algorithm run before the build is design-bounded, the
row above. The line is **temporal, not computational**: it does not matter how
clever the Python is or whether its output size depends on the data, only
whether it can run before the model is built. What is outside is work that
needs the solver's *answer* to decide the next row — lazy cut generation, a
solve loop — because there is no "before" for it to happen in.

This is a different property from how much a build costs, though the two meet at
the escape hatch. That is why an `escape:` island (#38) is admissible where a
registered Python helper was not: its extent is fixed by the preceding `where`
mask, it is terminal, and it is named in the file. Its **label budget is what keeps it
accountable** — an island's cost is bounded by what it may emit, declared and
enforced before any Python runs rather than discovered after it allocates.

An escape buys back the *relational* and *local* rules (it returns affine COO
rows — a running-sum island still emits affine rows, just O(T²) of them) but
never **degree**, because affine COO is what it returns. That refusal stands on
what an island *emits*, not on what a sink accepts, so it is unaffected by the
capability findings below.

### Capability is not the ceiling

The ceiling above is about **streamability** and is solver-independent. What a
*sink* can ingest is a separate axis, and conflating the two let one solver's
limits read as architectural law — "no sink carries the stream" described
HiGHS, not the architecture. Two findings, measured in
[docs/benchmarks.md](../benchmarks.md#sink-capabilities): SOS is
**solver-bounded** (HiGHS has no SOS concept at all, while `lp_file` carries it
as a text section and Gurobi natively), and what blocks quadratic is a
**conjunction** — HiGHS has integrality *and* a Hessian and refuses the pair —
so capability is not a flat set. The whole-Hessian handoff is an implementation
difference, not a rule-4 violation.

Making this a declared per-sink capability set, with `check` taking an optional
sink, is [ROADMAP Track 4](../ROADMAP.md#track-4--sink-capabilities).

## Composition (component libraries)

A component library is a fixed set of parametrised templates agreeing on a
port/flow convention, merged into one program, wired through a data connectivity
table and a single `group_sum` balance. **Topology is data, not structure** —
wiring a specific system is rows in a connectivity table, never generated YAML,
so structure is bounded by the number of component *types* while cardinality
lives entirely in data. Schema merge is therefore a pure **compose-then-build**
step producing one `MathSchema` before a single lower/stream pass (`linopy.extend`
is a linopy-lane shim; native merge is #30). Namespacing via qualified names is
the missing primitive (#29) — the port/flow surface stays deliberately shared, as
the coupling contract between templates — and signs and bidirectional flows need
bounds-as-expressions (#31).

Whatever genuinely is not data (variable port counts, runtime-unknown component
types) belongs in a thin layer emitting **more rows or more templates, never
per-instance YAML** — but **that layer has nothing *supported* to call**. A seam
does exist: `api.load_schema` accepts `dict | MathSchema`, so a programmatically
built model already goes through validation, expansion, resolution and dim
checking. It is just undocumented and unversioned, while rule 6 refuses a Python
modeling API and this section forbids generated YAML. Composition therefore
forces that contract earlier than the roadmap has it: not a general modeling API,
but a narrow, versioned way to emit declarations. Should anything ever be
blessed, it is the schema level and not the plan level — that much is settled;
what is not is whether to bless the seam at all, and namespacing (#29) or a
native schema merge (#30) is what would force the question.
