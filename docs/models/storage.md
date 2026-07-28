# storage

Dispatch plus a battery, and the only construct in the language whose cost is not obviously linear.

## The problem

State of charge links each snapshot to the one before it, cyclically:

$$\mathrm{soc}_s = \mathrm{soc}_{s-1} + \eta\,\mathrm{charge}_s - \mathrm{discharge}_s$$

with $s-1$ wrapping at the horizon, so the battery ends where it started.

## The model

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
    equations:
      - expression: sum(p, over=generator) + discharge - charge == load
  soc_balance:
    foreach: [snapshot]
    equations:
      # cyclic storage: soc wraps around the snapshot horizon
      - expression: soc == roll(soc, snapshot=1) + charge * 0.9 - discharge

objectives:
  total_cost:
    sense: minimize
    equations:
      - expression: p * cost
```

## What it exercises

`roll(soc, snapshot=1)` is the whole of it. One term reaches one position
back along `snapshot`, and `roll` wraps — the first snapshot reads the last,
which is what makes the storage cyclic without a boundary condition written
out by hand. `shift` is the same node with `wrap: false`, where positions
translated past the edge simply contribute nothing.

It is also the one plan shape whose cost is not obviously linear in the model
size, which is why it is named in *Not measured yet* in
[the benchmarks](../benchmarks.md).

---

[`examples/storage.yaml`](https://github.com/FBumann/farkas/blob/main/examples/storage.yaml) · back to [all models](index.md)
