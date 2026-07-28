# dispatch

Least-cost generation against a load profile — the smallest model that is still a model.

## The problem

Pick an output $p_{s,g}$ for every generator in every snapshot, so that the
fleet meets the load exactly and costs as little as possible:

$$\min \sum_{s,g} c_g \, p_{s,g}
\quad\text{s.t.}\quad \sum_g p_{s,g} = \ell_s ,\quad 0 \le p_{s,g} \le \bar p_g
\;\;\text{where}\;\; \bar p_g > 0$$

## The model

<!-- math:begin -->
<details>
<summary>The same model, as math</summary>

#### Sets

| Symbol | Meaning |
|---|---|
| $\mathcal{S}$ | index $s$ --- `snapshot` --- dispatch periods |
| $\mathcal{G}$ | index $g$ --- `generator` --- generating units |

#### Parameters

| Symbol | Meaning |
|---|---|
| $\bar p$ | `p_max` over $\mathcal{G}$ --- installed capacity |
| $\ell$ | `load` over $\mathcal{S}$ --- demand to be met |
| $c$ | `cost` over $\mathcal{G}$ --- marginal cost |

#### Variables

| Symbol | Meaning |
|---|---|
| $p$ | `p` over $\mathcal{S} \times \mathcal{G}$ --- output of generator $g$ in snapshot $s$ |

#### Objective

$$\begin{aligned}
\text{total\_cost:} \quad & \min & \sum_{s \in \mathcal{S},\ g \in \mathcal{G}} p_{s,g} \cdot c_{g}
\end{aligned}$$

#### Subject to

$$\begin{aligned}
\text{power\_balance:} \quad & \sum_{g \in \mathcal{G}} p_{s,g} & = \ell_{s} && \forall\, s \in \mathcal{S}
\end{aligned}$$

#### Variable domains

$$\begin{aligned}
\text{p:} \quad & 0 \le p_{s,g} & \le \bar p_{g} && \forall\, s \in \mathcal{S},\ g \in \mathcal{G} \,:\, \bar p_{g} > 0
\end{aligned}$$

</details>
<!-- math:end -->

```yaml
dimensions:
  snapshot:
    dtype: int
  generator:
    values: [wind, solar, gas]

parameters:
  p_max:
    dims: [generator]
  load:
    dims: [snapshot]
  cost:
    dims: [generator]

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
    equations:
      - expression: sum(p, over=generator) == load

objectives:
  total_cost:
    sense: minimize
    equations:
      - expression: p * cost
```

## What it exercises

`where: "p_max > 0"` is the one line worth pausing on. A generator with no
capacity gets **no columns at all** — not a column pinned to zero — so a
retired unit costs nothing to carry in the data. That is row absence, and it
is how sparsity is spelled throughout: see [`where`](../SPEC.md) in the
language reference.

---

[`examples/dispatch.yaml`](https://github.com/FBumann/farkas/blob/main/examples/dispatch.yaml) · back to [all models](index.md)
