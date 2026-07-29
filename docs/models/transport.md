# transport

A network: generators sit on buses, lines connect buses, and power balances at every bus.

## The problem

$$\sum_{g \in \mathrm{bus}} p_{s,g} \;+\; \sum_{\ell \to b} f_{s,\ell} \;-\; \sum_{\ell \,\text{from}\, b} f_{s,\ell} \;=\; \ell_{s,b}$$

## The model

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
