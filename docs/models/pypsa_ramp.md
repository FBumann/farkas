# PyPSA LOPF — rung 2, ramp limits

[Rung 1](pypsa_transport.md) plus a limit on how fast each generator may change output between snapshots.

> **✔ Verified against pypsa 1.2.4 (its own linopy 0.9.0)** — objective **18200**, matched to `rtol=1e-09`.

PyPSA states a ramp limit as a fraction of `p_nom` bounding the change between
consecutive snapshots, written from the *second* snapshot on — there is no
dispatch before the first for it to ramp from.

**The rung binds, and that took a redesign.** Rung 1's links run saturated,
which fixes every generator's output exactly; a ramp limit on that instance can
only make it infeasible, never change the answer. So this rung widens the
ratings to 200 and lets merit order pick the dispatch. Gas then moves
70 → 100 → 80 → 50, hitting its ±30 limit twice and calling oil on at the two
middle snapshots. Without the limits the same instance costs **17000**; with
them, 18200.

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

#### Parameters

| Symbol | Meaning |
|---|---|
| $p^{\mathrm{nom}}$ | `p_nom` over $\mathcal{G}$ |
| $\mathit{marginal\_cost}$ | `marginal_cost` over $\mathcal{G}$ |
| $\mathit{ramp\_limit\_up}$ | `ramp_limit_up` over $\mathcal{G}$ |
| $\mathit{ramp\_limit\_down}$ | `ramp_limit_down` over $\mathcal{G}$ |
| $\mathit{rating}$ | `rating` over $\mathcal{L}$ |
| $\mathit{neg\_rating}$ | `neg_rating` over $\mathcal{L}$ |
| $\mathit{load}$ | `load` over $\mathcal{T} \times \mathcal{B}$ |

#### Variables

| Symbol | Meaning |
|---|---|
| $p$ | `p` over $\mathcal{T} \times \mathcal{G}$ |
| $f$ | `f` over $\mathcal{T} \times \mathcal{L}$ |

$t \ominus k$ denotes cyclic translation: index $t-k$ taken modulo the size of the dimension (`roll`). Plain $t-k$ (`shift`) has no wraparound --- terms translated past the edge are simply absent.

#### Objective

**`total_cost`**

$$\min \sum_{t \in \mathcal{T},\enspace g \in \mathcal{G}} p_{t,g} \cdot \mathit{marginal\_cost}_{g}$$

#### Subject to

**`nodal_balance`**

$$\sum_{g \in \mathcal{G} \thinspace:\thinspace \mathrm{bus}(g) = b} p_{t,g} + \sum_{l \in \mathcal{L} \thinspace:\thinspace \mathrm{to}(l) = b} f_{t,l} - \left( \sum_{l \in \mathcal{L} \thinspace:\thinspace \mathrm{from}(l) = b} f_{t,l} \right) = \mathit{load}_{t,b} \qquad \forall\thinspace t \in \mathcal{T},\enspace b \in \mathcal{B}$$

**`ramp_up`**

$$p_{t,g} - p_{t \ominus 1,g} \le \mathit{ramp\_limit\_up}_{g} \cdot p^{\mathrm{nom}}_{g} \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G} \thinspace:\thinspace t > 0$$

**`ramp_down`**

$$p_{t \ominus 1,g} - p_{t,g} \le \mathit{ramp\_limit\_down}_{g} \cdot p^{\mathrm{nom}}_{g} \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G} \thinspace:\thinspace t > 0$$

#### Variable domains

**`p`**

$$0 \le p_{t,g} \le p^{\mathrm{nom}}_{g} \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G}$$

**`f`**

$$\mathit{neg\_rating}_{l} \le f_{t,l} \le \mathit{rating}_{l} \qquad \forall\thinspace t \in \mathcal{T},\enspace l \in \mathcal{L}$$

</details>
<!-- math:end -->

```yaml
# PyPSA linear optimal power flow, rung 2: rung 1 plus generator ramp limits.
# Optimum 18200.0, from PyPSA itself. See docs/ports.md.

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

constraints:
  nodal_balance:
    foreach: [snapshot, bus]
    expression: >-
      group_sum(p, over=generator, by=bus)
      + group_sum(f, over=link, by=to)
      - group_sum(f, over=link, by=from)
      == load

  # PyPSA states a ramp limit as a fraction of p_nom, so the right-hand side is
  # parameter arithmetic rather than a precomputed column. Both directions are
  # written from the *second* snapshot on: there is no dispatch before the
  # first for it to ramp from, which is what `where` says here. That gate is
  # also why `roll` is safe — its wrap lands on the one row that is excluded.
  ramp_up:
    foreach: [snapshot, generator]
    where: "snapshot > 0"
    expression: p - roll(p, snapshot=1) <= ramp_limit_up * p_nom

  ramp_down:
    foreach: [snapshot, generator]
    where: "snapshot > 0"
    expression: roll(p, snapshot=1) - p <= ramp_limit_down * p_nom

objectives:
  total_cost:
    sense: minimize
    expression: p * marginal_cost
```

`roll` is cyclic — it wraps the last snapshot onto the first — and that is safe
here only because `where: "snapshot > 0"` drops the one row where the wrap
would land. Written without the gate, this would silently be a *cyclic* ramp
constraint, which is a different model.

## Side by side

The reference builds the same network with PyPSA's own objects. The delta from
rung 1 is two keyword arguments:

```python
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pypsa

DATA = Path(__file__).resolve().parent.parent / 'data' / 'pypsa_ramp.json'


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
    # A ramp limit is the one rung that can make the instance infeasible rather
    # than merely different, and PyPSA reports that by leaving n.objective None
    # — which would otherwise surface as a TypeError three lines down.
    assert status == 'ok', f'{status}: {condition} — the ramp limits are tighter than the load swing'
    print(f'pypsa {pypsa.__version__}')
    print(f'objective {float(n.objective)!r}')
    print(f'duals {json.dumps({"nodal_balance": nodal_prices(n)})}')
    print(n.generators_t.p)
    return float(n.objective)


if __name__ == '__main__':
    main()
```

## What it exercises

`roll` — the first externally verified model in the corpus to use it. Also
parameter arithmetic on a constraint's right-hand side (`ramp_limit_up *
p_nom`), kept as arithmetic rather than a precomputed column so the file states
what PyPSA states.
