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
    equations:
      - expression: group_sum(p, over=generator, by=bus) + group_sum(f, over=link, by=to) - group_sum(f, over=link, by=from) + group_sum(p_dispatch, over=storage, by=bus) - group_sum(p_store, over=storage, by=bus) == load

  ramp:
    foreach: [snapshot, generator]
    where: "snapshot > 0"
    equations:
      - expression: p - roll(p, snapshot=1) <= ramp_limit_up * p_nom
      - expression: roll(p, snapshot=1) - p <= ramp_limit_down * p_nom

  # Charging is derated on the way in and discharging on the way out, so the
  # two efficiencies enter on opposite sides of the division. `standing_loss`
  # decays only what was *carried over* — PyPSA does not apply it to
  # soc_initial, which is why the first snapshot is its own equation rather
  # than a `roll` with a seeded value.
  energy_balance:
    foreach: [snapshot, storage]
    equations:
      - expression: soc == soc_initial + p_store * efficiency_store - p_dispatch / efficiency_dispatch
        where: "snapshot == 0"
      - expression: soc == roll(soc, snapshot=1) * (1 - standing_loss) + p_store * efficiency_store - p_dispatch / efficiency_dispatch
        where: "snapshot > 0"

objectives:
  total_cost:
    sense: minimize
    equations:
      - expression: p * marginal_cost
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
