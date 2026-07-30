# storage

Dispatch plus a battery, and the only construct in the language whose cost is not obviously linear.

## The problem

State of charge links each snapshot to the one before it, cyclically:

$$\mathrm{soc}_s = \mathrm{soc}_{s-1} + 0.9\,\mathrm{charge}_s - \mathrm{discharge}_s$$

with $s-1$ wrapping at the horizon, so the battery ends where it started. The
charging efficiency is written into the model as a literal `0.9` rather than
declared as a parameter — which is why it appears as a number here and not as
an $\eta$.

## The model

<!-- math:begin -->
<details markdown="1">
<summary>The same model, as math</summary>

#### Sets

| Symbol | Meaning |
|---|---|
| $\mathcal{S}$ | index $s$ --- `snapshot` --- dispatch periods, cyclic at the horizon |
| $\mathcal{G}$ | index $g$ --- `generator` --- generating units |

#### Parameters

| Symbol | Meaning |
|---|---|
| $\bar p$ | `p_max` over $\mathcal{G}$ --- installed capacity |
| $c$ | `cost` over $\mathcal{G}$ --- marginal cost |
| $\ell$ | `load` over $\mathcal{S}$ --- demand to be met |

#### Variables

| Symbol | Meaning |
|---|---|
| $p$ | `p` over $\mathcal{S} \times \mathcal{G}$ --- output of generator $g$ in snapshot $s$ |
| $\mathrm{charge}$ | `charge` over $\mathcal{S}$ --- energy into the store |
| $\mathrm{discharge}$ | `discharge` over $\mathcal{S}$ --- energy out of the store |
| $\mathrm{soc}$ | `soc` over $\mathcal{S}$ --- state of charge carried into the next snapshot |

$t \ominus k$ denotes cyclic translation: index $t-k$ taken modulo the size of the dimension (`roll`). Plain $t-k$ (`shift`) has no wraparound --- terms translated past the edge are simply absent.

#### Objective

**`total_cost`**

$$\min \sum_{s \in \mathcal{S},\enspace g \in \mathcal{G}} p_{s,g} \cdot c_{g}$$

#### Subject to

**`power_balance`**

$$\sum_{g \in \mathcal{G}} p_{s,g} + \mathrm{discharge}_{s} - \mathrm{charge}_{s} = \ell_{s} \qquad \forall\thinspace s \in \mathcal{S}$$

**`soc_balance`**

$$\mathrm{soc}_{s} = \mathrm{soc}_{s \ominus 1} + \mathrm{charge}_{s} \cdot 0.9 - \mathrm{discharge}_{s} \qquad \forall\thinspace s \in \mathcal{S}$$

#### Variable domains

**`p`**

$$0 \le p_{s,g} \le \bar p_{g} \qquad \forall\thinspace s \in \mathcal{S},\enspace g \in \mathcal{G}$$

**`charge`**

$$0 \le \mathrm{charge}_{s} \le 30 \qquad \forall\thinspace s \in \mathcal{S}$$

**`discharge`**

$$0 \le \mathrm{discharge}_{s} \le 30 \qquad \forall\thinspace s \in \mathcal{S}$$

**`soc`**

$$0 \le \mathrm{soc}_{s} \le 100 \qquad \forall\thinspace s \in \mathcal{S}$$

</details>
<!-- math:end -->

```yaml
dimensions:
  snapshot:
    dtype: int
  generator:
    dtype: str

parameters:
  p_max:
    dims: [generator]
  cost:
    dims: [generator]
  load:
    dims: [snapshot]

variables:
  p:
    foreach: [snapshot, generator]
    bounds:
      lower: 0
      upper: p_max
  charge:
    foreach: [snapshot]
    bounds:
      lower: 0
      upper: 30
  discharge:
    foreach: [snapshot]
    bounds:
      lower: 0
      upper: 30
  soc:
    foreach: [snapshot]
    bounds:
      lower: 0
      upper: 100

constraints:
  power_balance:
    foreach: [snapshot]
    expression: sum(p, over=generator) + discharge - charge == load
  soc_balance:
    foreach: [snapshot]
  # cyclic storage: soc wraps around the snapshot horizon
    expression: soc == shift(soc, over=snapshot, by=1, edge=wrap) + charge * 0.9 - discharge

objectives:
  total_cost:
    sense: minimize
    expression: p * cost
```

## What it exercises

`shift(soc, over=snapshot, by=1, edge=wrap)` is the whole of it. One term reaches one position
back along `snapshot`, and `roll` wraps — the first snapshot reads the last,
which is what makes the storage cyclic without a boundary condition written
out by hand. `shift` is the same node with `wrap: false`, where positions
translated past the edge simply contribute nothing.

It is also the one plan shape whose cost is not obviously linear in the model
size, which is why it is named in *Not measured yet* in
[the benchmarks](../benchmarks.md).

---

[`examples/storage.yaml`](https://github.com/FBumann/lpspec/blob/main/examples/storage.yaml) · back to [all models](index.md)
