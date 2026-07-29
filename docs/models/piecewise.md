# piecewise

Per-generator convex cost curves, expanded into a λ-formulation.

## The problem

Each generator gets its own breakpoint list, so the curve varies per unit —
something a flat breakpoint list cannot express:

$$p_g = \sum_k \lambda_{g,k}\, x_{g,k}, \quad
\mathrm{cost}_g = \sum_k \lambda_{g,k}\, y_{g,k}, \quad
\sum_k \lambda_{g,k} = 1, \quad \lambda \ge 0$$

## The model

<!-- math:begin -->
<details markdown="1">
<summary>The same model, as math</summary>

#### Sets

| Symbol | Meaning |
|---|---|
| $\mathcal{T}$ | index $t$ --- `snapshot` --- dispatch periods |
| $\mathcal{G}$ | index $g$ --- `generator` --- dispatchable units |
| $\mathcal{K}$ | index $k$ --- `bp` --- breakpoints of the cost curve |

#### Parameters

| Symbol | Meaning |
|---|---|
| $p^{\mathrm{max}}$ | `p_max` over $\mathcal{G}$ --- maximum dispatch |
| $\mathit{load}$ | `load` over $\mathcal{T}$ --- demand to be met |
| $x$ | `bp_x` over $\mathcal{G} \times \mathcal{K}$ --- breakpoint dispatch levels |
| $y$ | `bp_y` over $\mathcal{G} \times \mathcal{K}$ --- cost at each breakpoint |

#### Variables

| Symbol | Meaning |
|---|---|
| $p$ | `p` over $\mathcal{T} \times \mathcal{G}$ --- dispatched power |
| $\mathrm{cost}$ | `op_cost` over $\mathcal{T} \times \mathcal{G}$ --- operating cost, piecewise-linear in dispatch |
| $\lambda$ | `cost_curve_lam` over $\mathcal{T} \times \mathcal{G} \times \mathcal{K}$ --- convex-combination weight on breakpoint $k$ |

#### Objective

**`total_cost`**

$$\min \sum_{t \in \mathcal{T},\enspace g \in \mathcal{G}} \mathrm{cost}_{t,g}$$

#### Subject to

**`balance`**

$$\sum_{g \in \mathcal{G}} p_{t,g} = \mathit{load}_{t} \qquad \forall\thinspace t \in \mathcal{T}$$

**`cost_curve_convexity`**

$$\sum_{k \in \mathcal{K}} \lambda_{t,g,k} = 1 \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G}$$

**`cost_curve_link0`**

$$p_{t,g} = \sum_{k \in \mathcal{K}} \lambda_{t,g,k} \cdot x_{g,k} \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G}$$

**`cost_curve_link1`**

$$\mathrm{cost}_{t,g} = \sum_{k \in \mathcal{K}} \lambda_{t,g,k} \cdot y_{g,k} \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G}$$

#### Variable domains

**`p`**

$$0 \le p_{t,g} \le p^{\mathrm{max}}_{g} \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G}$$

**`op_cost`**

$$\mathrm{cost}_{t,g} \ge 0 \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G}$$

**`cost_curve_lam`**

$$0 \le \lambda_{t,g,k} \le 1 \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G},\enspace k \in \mathcal{K}$$

</details>
<!-- math:end -->

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
    dims: [generator, bp]  # per-generator breakpoint positions
  bp_y:
    dims: [generator, bp]  # per-generator cost at each breakpoint

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
