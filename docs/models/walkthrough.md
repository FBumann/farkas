# walkthrough

The dispatch model plus a macro and a named expression — the one used to print every pipeline stage.

## The model

<!-- math:begin -->
<details markdown="1">
<summary>The same model, as math</summary>

#### Sets

| Symbol | Meaning |
|---|---|
| $\mathcal{S}$ | index $s$ --- `snapshot` --- dispatch periods |
| $\mathcal{G}$ | index $g$ --- `generator` --- generating units, including the retired one |

#### Parameters

| Symbol | Meaning |
|---|---|
| $\bar p$ | `p_max` over $\mathcal{G}$ --- installed capacity, zero for a retired unit |
| $\ell$ | `load` over $\mathcal{S}$ --- demand to be met |
| $c$ | `cost` over $\mathcal{G}$ --- marginal cost |

#### Variables

| Symbol | Meaning |
|---|---|
| $p$ | `p` over $\mathcal{S} \times \mathcal{G}$ --- output of generator $g$ in snapshot $s$ |

#### Objective

**`total_cost`**

$$\min \sum_{s \in \mathcal{S}} \sum_{g \in \mathcal{G}} p_{s,g} \cdot c_{g}$$

#### Subject to

**`power_balance`**

$$\sum_{g \in \mathcal{G}} p_{s,g} = \ell_{s} \qquad \forall\thinspace s \in \mathcal{S}$$

#### Variable domains

**`p`**

$$0 \le p_{s,g} \le \bar p_{g} \qquad \forall\thinspace s \in \mathcal{S},\enspace g \in \mathcal{G} \thinspace:\thinspace \bar p_{g} > 0$$

</details>
<!-- math:end -->

```yaml
# The dispatch model of README.md, plus one macro and one named expression —
# small enough to print in full, complete enough that every pipeline stage in
# examples/walkthrough.py has something to show.

dimensions:
  snapshot:
    dtype: int
  generator:
    # oil is declared but retired (p_max = 0) — the `where` below gives it no
    # columns at all, so the built model is smaller than the coord product.
    values: [wind, solar, gas, oil]

parameters:
  p_max: {dims: [generator]}
  load: {dims: [snapshot]}
  cost: {dims: [generator]}

# Tier 2 — free composition. Neither block survives past expansion.py, so no
# backend ever sees them (docs/ARCHITECTURE.md, hard rule 1).
expressions:
  total_supply: sum(p, over=generator)

macros:
  weighted_sum:
    args: [array, weights]
    kwargs: [over]
    template: sum(array * weights, over=over)

variables:
  p:
    foreach: [snapshot, generator]
    where: "p_max > 0"
    bounds:
      lower: 0
      upper: p_max

constraints:
  power_balance:
    foreach: [snapshot]
    expression: total_supply == load

objectives:
  total_cost:
    sense: minimize
    expression: weighted_sum(p, cost, over=generator)
```

## What it exercises

This is the model behind `python examples/walkthrough.py`, which runs it
through every stage — YAML → schema → core AST → logical plan → model frames →
LP text → solution — printing what each stage produces, then two models the
language refuses and why. The committed output is
[examples/walkthrough.out](https://github.com/FBumann/farkas/blob/main/examples/walkthrough.out)
if you would rather read than run.

It is the only model here that uses **tier 2**: a macro and a named
expression, neither of which survives past expansion. Nothing downstream of
`expansion.py` knows they existed, which is what makes them free.

---

[`examples/walkthrough.yaml`](https://github.com/FBumann/farkas/blob/main/examples/walkthrough.yaml) · back to [all models](index.md)
