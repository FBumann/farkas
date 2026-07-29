# PyPSA LOPF — rung 3, storage

[Rung 2](pypsa_ramp.md) plus a `StorageUnit` carrying energy between snapshots.

> **✔ Verified against pypsa 1.2.4 (its own linopy 0.9.0)** — objective **15253.178322993519**, matched to `rtol=1e-09`.

Non-cyclic: the horizon starts at `soc_initial` and its end is free. Closing
that loop is [rung 4](pypsa_cyclic_storage.md), kept separate so it can fail on
its own.

The battery sits at `south`, where the expensive oil generator is. It displaces
oil **entirely** — every snapshot runs oil at zero — and drains to empty by the
third, which is what a free end-of-horizon buys you.

## The model

<!-- math:begin -->
<details markdown="1">
<summary>The same model, as math</summary>

#### Sets

| Symbol | Meaning |
|---|---|
| $\mathcal{T}$ | index $t$ --- `snapshot` |
| $\mathcal{B}$ | index $b$ --- `bus` |
| $\mathcal{G}$ | index $g$ --- `generator` with $\mathrm{bus}: \mathcal{G} \to \mathcal{B}$ |
| $\mathcal{L}$ | index $l$ --- `link` with $\mathrm{from}: \mathcal{L} \to \mathcal{B},\enspace \mathrm{to}: \mathcal{L} \to \mathcal{B}$ |
| $\mathcal{S}$ | index $s$ --- `storage` with $\mathrm{bus}: \mathcal{S} \to \mathcal{B}$ |

#### Parameters

| Symbol | Meaning |
|---|---|
| $p^{\mathrm{nom}}$ | `p_nom` over $\mathcal{G}$ |
| $\mathit{marginal\_cost}$ | `marginal_cost` over $\mathcal{G}$ |
| $\mathit{ramp\_limit\_up}$ | `ramp_limit_up` over $\mathcal{G}$ |
| $\mathit{ramp\_limit\_down}$ | `ramp_limit_down` over $\mathcal{G}$ |
| $\mathit{rating}$ | `rating` over $\mathcal{L}$ |
| $\mathit{neg\_rating}$ | `neg_rating` over $\mathcal{L}$ |
| $\mathit{storage}^{\mathrm{p,nom}}$ | `storage_p_nom` over $\mathcal{S}$ |
| $\mathit{soc}^{\mathrm{max}}$ | `soc_max` over $\mathcal{S}$ |
| $\mathit{soc}^{\mathrm{initial}}$ | `soc_initial` over $\mathcal{S}$ |
| $\mathit{efficiency\_store}$ | `efficiency_store` over $\mathcal{S}$ |
| $\mathit{efficiency\_dispatch}$ | `efficiency_dispatch` over $\mathcal{S}$ |
| $\mathit{standing\_loss}$ | `standing_loss` over $\mathcal{S}$ |
| $\mathit{load}$ | `load` over $\mathcal{T} \times \mathcal{B}$ |

#### Variables

| Symbol | Meaning |
|---|---|
| $p$ | `p` over $\mathcal{T} \times \mathcal{G}$ |
| $f$ | `f` over $\mathcal{T} \times \mathcal{L}$ |
| $p^{\mathrm{dispatch}}$ | `p_dispatch` over $\mathcal{T} \times \mathcal{S}$ |
| $p^{\mathrm{store}}$ | `p_store` over $\mathcal{T} \times \mathcal{S}$ |
| $\mathit{soc}$ | `soc` over $\mathcal{T} \times \mathcal{S}$ |

$t \ominus k$ denotes cyclic translation: index $t-k$ taken modulo the size of the dimension (`roll`). Plain $t-k$ (`shift`) has no wraparound --- terms translated past the edge are simply absent.

#### Objective

**`total_cost`**

$$\min \sum_{t \in \mathcal{T},\enspace g \in \mathcal{G}} p_{t,g} \cdot \mathit{marginal\_cost}_{g}$$

#### Subject to

**`nodal_balance`**

$$\sum_{g \in \mathcal{G} \thinspace:\thinspace \mathrm{bus}(g) = b} p_{t,g} + \sum_{l \in \mathcal{L} \thinspace:\thinspace \mathrm{to}(l) = b} f_{t,l} - \left( \sum_{l \in \mathcal{L} \thinspace:\thinspace \mathrm{from}(l) = b} f_{t,l} \right) + \sum_{s \in \mathcal{S} \thinspace:\thinspace \mathrm{bus}(s) = b} p^{\mathrm{dispatch}}_{t,s} - \left( \sum_{s \in \mathcal{S} \thinspace:\thinspace \mathrm{bus}(s) = b} p^{\mathrm{store}}_{t,s} \right) = \mathit{load}_{t,b} \qquad \forall\thinspace t \in \mathcal{T},\enspace b \in \mathcal{B}$$

**`ramp_up`**

$$p_{t,g} - p_{t \ominus 1,g} \le \mathit{ramp\_limit\_up}_{g} \cdot p^{\mathrm{nom}}_{g} \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G} \thinspace:\thinspace t > 0$$

**`ramp_down`**

$$p_{t \ominus 1,g} - p_{t,g} \le \mathit{ramp\_limit\_down}_{g} \cdot p^{\mathrm{nom}}_{g} \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G} \thinspace:\thinspace t > 0$$

**`energy_balance_initial`**

$$\mathit{soc}_{t,s} = \mathit{soc}^{\mathrm{initial}}_{s} + p^{\mathrm{store}}_{t,s} \cdot \mathit{efficiency\_store}_{s} - \frac{p^{\mathrm{dispatch}}_{t,s}}{\mathit{efficiency\_dispatch}_{s}} \qquad \forall\thinspace t \in \mathcal{T},\enspace s \in \mathcal{S} \thinspace:\thinspace t = 0$$

**`energy_balance`**

$$\mathit{soc}_{t,s} = \mathit{soc}_{t \ominus 1,s} \cdot \left( 1 - \mathit{standing\_loss}_{s} \right) + p^{\mathrm{store}}_{t,s} \cdot \mathit{efficiency\_store}_{s} - \frac{p^{\mathrm{dispatch}}_{t,s}}{\mathit{efficiency\_dispatch}_{s}} \qquad \forall\thinspace t \in \mathcal{T},\enspace s \in \mathcal{S} \thinspace:\thinspace t > 0$$

#### Variable domains

**`p`**

$$0 \le p_{t,g} \le p^{\mathrm{nom}}_{g} \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G}$$

**`f`**

$$\mathit{neg\_rating}_{l} \le f_{t,l} \le \mathit{rating}_{l} \qquad \forall\thinspace t \in \mathcal{T},\enspace l \in \mathcal{L}$$

**`p_dispatch`**

$$0 \le p^{\mathrm{dispatch}}_{t,s} \le \mathit{storage}^{\mathrm{p,nom}}_{s} \qquad \forall\thinspace t \in \mathcal{T},\enspace s \in \mathcal{S}$$

**`p_store`**

$$0 \le p^{\mathrm{store}}_{t,s} \le \mathit{storage}^{\mathrm{p,nom}}_{s} \qquad \forall\thinspace t \in \mathcal{T},\enspace s \in \mathcal{S}$$

**`soc`**

$$0 \le \mathit{soc}_{t,s} \le \mathit{soc}^{\mathrm{max}}_{s} \qquad \forall\thinspace t \in \mathcal{T},\enspace s \in \mathcal{S}$$

</details>
<!-- math:end -->

```yaml
# PyPSA linear optimal power flow, rung 3: rung 2 plus a storage unit carrying
# energy between snapshots. Non-cyclic — the horizon starts at soc_initial and
# ends free. Optimum 15253.178322993519, from PyPSA itself. See docs/ports.md.

dimensions:
  snapshot:
    dtype: int
  bus:
    dtype: str
  generator:
    dtype: str
    coords: [bus]  # every generator sits on a bus
  link:
    dtype: str
    coords: {from: bus, to: bus}  # both endpoints are buses
  storage:
    dtype: str
    coords: [bus]  # a storage unit sits on a bus too

parameters:
  p_nom:
    dims: [generator]
  marginal_cost:
    dims: [generator]
  ramp_limit_up:
    dims: [generator]
  ramp_limit_down:
    dims: [generator]
  rating:
    dims: [link]
  neg_rating:
    dims: [link]
  storage_p_nom:
    dims: [storage]
  # p_nom * max_hours in PyPSA. Carried as a column because a bound takes a
  # name or a number, never arithmetic (#31) — the third model to want that.
  soc_max:
    dims: [storage]
  soc_initial:
    dims: [storage]
  efficiency_store:
    dims: [storage]
  efficiency_dispatch:
    dims: [storage]
  standing_loss:
    dims: [storage]
  load:
    dims: [snapshot, bus]

variables:
  p:
    foreach: [snapshot, generator]
    bounds:
      lower: 0
      upper: p_nom
  f:
    foreach: [snapshot, link]
    bounds:
      lower: neg_rating
      upper: rating
  # PyPSA splits a storage unit's power into two non-negative variables rather
  # than one signed one, so the two efficiencies can differ.
  p_dispatch:
    foreach: [snapshot, storage]
    bounds:
      lower: 0
      upper: storage_p_nom
  p_store:
    foreach: [snapshot, storage]
    bounds:
      lower: 0
      upper: storage_p_nom
  soc:
    foreach: [snapshot, storage]
    bounds:
      lower: 0
      upper: soc_max

constraints:
  nodal_balance:
    foreach: [snapshot, bus]
    expression: >-
      group_sum(p, over=generator, by=bus)
      + group_sum(f, over=link, by=to)
      - group_sum(f, over=link, by=from)
      + group_sum(p_dispatch, over=storage, by=bus)
      - group_sum(p_store, over=storage, by=bus)
      == load

  ramp_up:
    foreach: [snapshot, generator]
    where: "snapshot > 0"
    expression: p - roll(p, snapshot=1) <= ramp_limit_up * p_nom

  ramp_down:
    foreach: [snapshot, generator]
    where: "snapshot > 0"
    expression: roll(p, snapshot=1) - p <= ramp_limit_down * p_nom

  # Charging is derated on the way in and discharging on the way out, so the
  # two efficiencies enter on opposite sides of the division. `standing_loss`
  # decays only what was *carried over* — PyPSA does not apply it to
  # soc_initial, which is why the first snapshot is its own equation rather
  # than a `roll` with a seeded value.
  energy_balance_initial:
    foreach: [snapshot, storage]
    where: "snapshot == 0"
    expression: >-
      soc == soc_initial
      + p_store * efficiency_store
      - p_dispatch / efficiency_dispatch

  energy_balance:
    foreach: [snapshot, storage]
    where: "snapshot > 0"
    expression: >-
      soc == roll(soc, snapshot=1) * (1 - standing_loss)
      + p_store * efficiency_store
      - p_dispatch / efficiency_dispatch

objectives:
  total_cost:
    sense: minimize
    expression: p * marginal_cost
```

**Two efficiencies, on opposite sides of the division.** PyPSA splits a storage
unit's power into two non-negative variables rather than one signed one,
precisely so charging and discharging can be derated differently:
`p_store * efficiency_store` on the way in, `p_dispatch / efficiency_dispatch`
on the way out.

**`standing_loss` decays only what was carried over.** PyPSA does not apply it
to `soc_initial`, so the first snapshot is its own equation rather than a
`roll` with a seeded value. Applying the loss to the seed as well — a one-token
change, and the reading most people would call obvious — moves the objective to
**15272.957445031367**, about 20 out of 15253. Wrong by 0.13%: far too small to
notice by eye on a plot, far too large to be rounding. That gap is the entire
argument for checking against somebody else's number instead of against a
result that merely looks sensible.

## Side by side

```python
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pypsa

DATA = Path(__file__).resolve().parent.parent / 'data' / 'pypsa_storage.json'


def build(data: dict[str, dict[str, list]]) -> pypsa.Network:
    """The port's tables as a PyPSA network, column for column."""
    n = pypsa.Network()
    n.set_snapshots(data['snapshot']['snapshot'])
    n.add('Bus', data['bus']['bus'])

    n.add(
        'Generator',
        data['generator']['generator'],
        bus=data['generator']['bus'],
        p_nom=data['p_nom']['value'],
        marginal_cost=data['marginal_cost']['value'],
        ramp_limit_up=data['ramp_limit_up']['value'],
        ramp_limit_down=data['ramp_limit_down']['value'],
    )
    n.add(
        'Link',
        data['link']['link'],
        bus0=data['link']['from'],
        bus1=data['link']['to'],
        p_nom=data['rating']['value'],
        p_min_pu=-1.0,
        efficiency=1.0,
    )
    # max_hours is the ratio PyPSA stores; the port carries the product it
    # implies (soc_max) because a bound there takes a name, not arithmetic.
    p_nom = data['storage_p_nom']['value']
    n.add(
        'StorageUnit',
        data['storage']['storage'],
        bus=data['storage']['bus'],
        p_nom=p_nom,
        max_hours=[m / p for m, p in zip(data['soc_max']['value'], p_nom, strict=True)],
        state_of_charge_initial=data['soc_initial']['value'],
        efficiency_store=data['efficiency_store']['value'],
        efficiency_dispatch=data['efficiency_dispatch']['value'],
        standing_loss=data['standing_loss']['value'],
        cyclic_state_of_charge=False,
    )

    load = pd.DataFrame(data['load']).pivot(index='snapshot', columns='bus', values='value')
    for bus in data['bus']['bus']:
        n.add('Load', f'load_{bus}', bus=bus, p_set=load[bus])
    return n


def nodal_prices(n: pypsa.Network) -> dict[str, list]:
    """PyPSA's marginal price per (snapshot, bus), tidy — the dual of the nodal
    balance, and the output this community reads most often after the cost.

    Recorded in references.json so the port is checked on a whole *vector*, not
    just the objective. A sign convention that disagreed would be invisible to
    a scalar comparison and wrong in every reported price.
    """
    mp = n.buses_t.marginal_price
    return {
        'snapshot': [s for s in mp.index for _ in mp.columns],
        'bus': [b for _ in mp.index for b in mp.columns],
        'value': [float(v) for row in mp.to_numpy() for v in row],
    }


def main() -> float:
    n = build(json.loads(DATA.read_text()))
    status, condition = n.optimize(solver_name='highs')
    assert status == 'ok', f'{status}: {condition}'
    print(f'pypsa {pypsa.__version__}')
    print(f'objective {float(n.objective)!r}')
    print(f'duals {json.dumps({"nodal_balance": nodal_prices(n)})}')
    print(n.generators_t.p)
    print(n.storage_units_t.state_of_charge)
    return float(n.objective)


if __name__ == '__main__':
    main()
```

## What it exercises

`roll` across a boundary condition, division of a variable by a parameter, and
a five-term `group_sum` nodal balance — generators, both ends of every link,
and both directions of storage, all projected onto `bus`.

It also asks for [#31](https://github.com/FBumann/farkas/issues/31) a third
time: `soc_max` is `p_nom × max_hours` in PyPSA, and a bound here takes a name
or a number, so the product ships as a column.
