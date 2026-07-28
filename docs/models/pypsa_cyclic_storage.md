# PyPSA LOPF — rung 4, cyclic storage

[Rung 3](pypsa_storage.md) with the horizon closed on itself: the first snapshot's state of charge carries over from the *last*.

> **✔ Verified against pypsa 1.2.4 (its own linopy 0.9.0)** — objective **17228.77962151063**, matched to `rtol=1e-09`.

**The rung that makes the model smaller.** Rung 3 needs two equations for the
energy balance — one seeding the first snapshot from `soc_initial`, one rolling
every other. Closing the cycle *removes the first*. `roll` is already cyclic, so
wrapping snapshot 0 onto the last is what it does unguarded, and deleting the
`where` is the entire change:

```diff
-      - expression: soc == soc_initial + p_store * ... - p_dispatch / ...
-        where: "snapshot == 0"
-      - expression: soc == roll(soc, snapshot=1) * (1 - standing_loss) + ...
-        where: "snapshot > 0"
+      - expression: soc == roll(soc, snapshot=1) * (1 - standing_loss) + ...
```

`soc_initial` leaves the instance with it — a cyclic horizon has no seed to
give. In PyPSA the same change is `cyclic_state_of_charge=True`, which is
shorter still; the difference is that theirs is a flag on a component and ours
is the absence of a special case.

Closing the loop costs money: **17228.78** against rung 3's **15253.18**. The
battery can no longer end the horizon empty, so it has to buy back what it
spends.

## The model

```yaml
# PyPSA linear optimal power flow, rung 4: rung 3's storage, closed into a
# cycle — the first snapshot's state of charge carries over from the last.
# Optimum 17228.77962151063, from PyPSA itself. See docs/ports.md.

dimensions:
  snapshot:
    dtype: int
  bus:
    dtype: str
  generator:
    dtype: str
    coords: [bus]                  # every generator sits on a bus
  link:
    dtype: str
    coords: {from: bus, to: bus}   # both endpoints are buses
  storage:
    dtype: str
    coords: [bus]                  # a storage unit sits on a bus too

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
  soc_max:
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
    equations:
      - expression: group_sum(p, over=generator, by=bus) + group_sum(f, over=link, by=to) - group_sum(f, over=link, by=from) + group_sum(p_dispatch, over=storage, by=bus) - group_sum(p_store, over=storage, by=bus) == load

  ramp:
    foreach: [snapshot, generator]
    where: "snapshot > 0"
    equations:
      - expression: p - roll(p, snapshot=1) <= ramp_limit_up * p_nom
      - expression: roll(p, snapshot=1) - p <= ramp_limit_down * p_nom

  # Rung 3 needed two equations here: one seeding the first snapshot from
  # soc_initial, one rolling every other. Closing the cycle *removes* the
  # first — `roll` is already cyclic, so the wrap onto the last snapshot is
  # what it does unguarded, and dropping the `where` is the whole change.
  energy_balance:
    foreach: [snapshot, storage]
    equations:
      - expression: soc == roll(soc, snapshot=1) * (1 - standing_loss) + p_store * efficiency_store - p_dispatch / efficiency_dispatch

objectives:
  total_cost:
    sense: minimize
    equations:
      - expression: p * marginal_cost
```

## Side by side

```python
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pypsa

DATA = Path(__file__).resolve().parent.parent / 'data' / 'pypsa_cyclic_storage.json'


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
        efficiency_store=data['efficiency_store']['value'],
        efficiency_dispatch=data['efficiency_dispatch']['value'],
        standing_loss=data['standing_loss']['value'],
        cyclic_state_of_charge=True,
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

The same constructs as [rung 3](pypsa_storage.md) — `roll`, division by a
parameter, a five-term `group_sum` balance — with one fewer equation and one
fewer parameter. Worth reading the two side by side: the language's cyclic case
is its default, and the *acyclic* one is what needs the extra clause.
