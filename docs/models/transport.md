# transport

A network: generators sit on buses, lines connect buses, and power balances at every bus.

## The problem

$$\sum_{g \,:\, \mathrm{bus}(g) = b} p_{s,g} \;+\; \sum_{\ell \,:\, \mathrm{to}(\ell) = b} f_{s,\ell} \;-\; \sum_{\ell \,:\, \mathrm{from}(\ell) = b} f_{s,\ell} \;=\; d_{s,b}$$

Each sum is over the lines or generators a *coordinate map* sends to bus $b$ —
$\mathrm{bus}$, $\mathrm{to}$ and $\mathrm{from}$ are the coordinates the
dimensions declare, not sets in their own right. Load is $d$ here, because
$\ell$ is already the line index.

## The model

<!-- math:begin -->
<details>
<summary>The same model, as math</summary>

#### Sets

| Symbol | Meaning |
|---|---|
| $\mathcal{S}$ | index $s$ --- `snapshot` --- dispatch periods |
| $\mathcal{G}$ | index $g$ --- `generator` with $\mathrm{bus}: \mathcal{G} \to \mathcal{B}$ --- generating units |
| $\mathcal{B}$ | index $b$ --- `bus` --- network nodes |
| $\mathcal{L}$ | index $\ell$ --- `line` with $\mathrm{from}: \mathcal{L} \to \mathcal{B},\ \mathrm{to}: \mathcal{L} \to \mathcal{B}$ --- transmission lines, each joining two buses |

#### Parameters

| Symbol | Meaning |
|---|---|
| $\bar p$ | `p_max` over $\mathcal{G}$ --- installed capacity |
| $c$ | `cost` over $\mathcal{G}$ --- marginal cost |
| $\bar f$ | `cap` over $\mathcal{L}$ --- forward transmission limit |
| $\underline{f}$ | `neg_cap` over $\mathcal{L}$ --- reverse transmission limit |
| $d$ | `load` over $\mathcal{S} \times \mathcal{B}$ --- demand at each bus |

#### Variables

| Symbol | Meaning |
|---|---|
| $p$ | `p` over $\mathcal{S} \times \mathcal{G}$ --- output of generator $g$ in snapshot $s$ |
| $f$ | `f` over $\mathcal{S} \times \mathcal{L}$ --- flow on line $\ell$, signed towards its `to` bus |

#### Objective

$$\begin{aligned}
\text{total\_cost:} \quad & \min & \sum_{s \in \mathcal{S},\ g \in \mathcal{G}} p_{s,g} \cdot c_{g}
\end{aligned}$$

#### Subject to

$$\begin{aligned}
\text{balance:} \quad & \sum_{g \in \mathcal{G} \,:\, \mathrm{bus}(g) = b} p_{s,g} + \sum_{\ell \in \mathcal{L} \,:\, \mathrm{to}(\ell) = b} f_{s,\ell} - \left( \sum_{\ell \in \mathcal{L} \,:\, \mathrm{from}(\ell) = b} f_{s,\ell} \right) & = d_{s,b} && \forall\, s \in \mathcal{S},\ b \in \mathcal{B}
\end{aligned}$$

#### Variable domains

$$\begin{aligned}
\text{p:} \quad & 0 \le p_{s,g} & \le \bar p_{g} && \forall\, s \in \mathcal{S},\ g \in \mathcal{G} \\
\text{f:} \quad & \underline{f}_{\ell} \le f_{s,\ell} & \le \bar f_{\ell} && \forall\, s \in \mathcal{S},\ \ell \in \mathcal{L}
\end{aligned}$$

</details>
<!-- math:end -->

```yaml
dimensions:
  snapshot:
    dtype: int
  generator:
    dtype: str
    coords: [bus]  # every generator sits on a bus
  bus:
    dtype: str
  line:
    dtype: str
    coords: {from: bus, to: bus}  # both endpoints are buses

parameters:
  p_max:
    dims: [generator]
  cost:
    dims: [generator]
  cap:
    dims: [line]
  neg_cap:
    dims: [line]
  load:
    dims: [snapshot, bus]

variables:
  p:
    foreach: [snapshot, generator]
    bounds:
      lower: 0
      upper: p_max
  f:
    foreach: [snapshot, line]
    bounds:
      lower: neg_cap
      upper: cap

constraints:
  balance:
    foreach: [snapshot, bus]
    equations:
      - expression: >-
          group_sum(p, over=generator, by=bus)
          + group_sum(f, over=line, by=to)
          - group_sum(f, over=line, by=from)
          == load

objectives:
  total_cost:
    sense: minimize
    equations:
      - expression: p * cost
```

## What it exercises

Three `group_sum` calls, and they are what a network *is* in this language.
A dimension can carry **coordinates** — `generator` carries `bus`, `line`
carries `from` and `to` — and `group_sum(f, over=line, by=to)` sums along a
line's `to` coordinate, landing the result on `bus`. The same `f` is summed
twice through two different coordinates, once as an inflow and once as an
outflow.

No adjacency matrix, and no join written by the modeller: the topology is
data on the dimension.

---

[`examples/transport.yaml`](https://github.com/FBumann/farkas/blob/main/examples/transport.yaml) · back to [all models](index.md)
