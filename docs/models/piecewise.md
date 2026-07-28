# piecewise

Per-generator convex cost curves, expanded into a λ-formulation.

## The problem

Each generator gets its own breakpoint list, so the curve varies per unit —
something a flat breakpoint list cannot express:

$$p_g = \sum_k \lambda_{g,k}\, x_{g,k}, \quad
\mathrm{cost}_g = \sum_k \lambda_{g,k}\, y_{g,k}, \quad
\sum_k \lambda_{g,k} = 1, \quad \lambda \ge 0$$

## The model

```yaml
# Dispatch with piecewise-linear generation costs via the piecewise: block.
#
# Each generator has its own convex cost curve: the breakpoint parameters
# carry [generator, bp], so curves vary per generator — something flat
# breakpoint lists cannot express. With convex: true the expansion emits no
# binaries (pure-LP convex hull, exact for convex curves under minimisation)
# and the model stays relational-eligible as an LP. Drop convex: true for
# nonconvex curves — the expansion then adds segment binaries + adjacency,
# and the model becomes a (still relational-eligible) MILP.

dimensions:
  snapshot:
    dtype: int
  generator:
    dtype: str
  bp:
    dtype: int

parameters:
  p_max:
    dims: [generator]
  load:
    dims: [snapshot]
  bp_x:
    dims: [generator, bp]   # per-generator breakpoint positions
  bp_y:
    dims: [generator, bp]   # per-generator cost at each breakpoint

variables:
  p:
    foreach: [snapshot, generator]
    bounds:
      lower: 0
      upper: p_max
  op_cost:
    foreach: [snapshot, generator]
    bounds:
      lower: 0

piecewise:
  cost_curve:
    over: bp
    links:
      - [p, bp_x]
      - [op_cost, bp_y]
    convex: true

constraints:
  balance:
    foreach: [snapshot]
    equations:
      - expression: sum(p, over=generator) == load

objectives:
  total_cost:
    sense: minimize
    equations:
      - expression: op_cost
```

## What it exercises

`piecewise:` is a **declaration, not a helper** — it expands before lowering
into the λ-formulation above. With `convex: true` the expansion emits no
binaries at all: the convex hull is exact for a convex curve under
minimisation, so the model stays a pure LP. Drop `convex: true` and the
expansion adds segment binaries and adjacency constraints, and the model
becomes a MILP that is still entirely inside the relational subset.

By the time the logical plan exists there is nothing left called *piecewise* —
which is why the construct matrix reads it from the surface declaration.

---

[`examples/piecewise.yaml`](https://github.com/FBumann/farkas/blob/main/examples/piecewise.yaml) · back to [all models](index.md)
