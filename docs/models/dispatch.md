# dispatch

Least-cost generation against a load profile — the smallest model that is still a model.

## The problem

Pick an output $p_{s,g}$ for every generator in every snapshot, so that the
fleet meets the load exactly and costs as little as possible:

$$\min \sum_{s,g} c_g \, p_{s,g}
\quad\text{s.t.}\quad \sum_g p_{s,g} = \ell_s ,\quad 0 \le p_{s,g} \le \bar p_g$$

## The model

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
