# Dantzig transport with economies of scale

GAMS model library `trnspwl`: the same shipping problem, but a big consignment is cheaper per unit — cost grows as `sqrt(x)`, not linearly.

> **✔ Verified against linopy 0.9.0's own `add_piecewise_formulation`** — objective **8.786852757777865**, matched to `rtol=1e-09`.

**The corpus's `piecewise` entry**, and the last hole in the construct matrix.

It is also the port with the sharpest kind of independence. Every other
reference is independent of farkas because it is a different program; this one
is independent of *the construct under test*. `piecewise:` and linopy's
`add_piecewise_formulation` are two separate implementations of the same
λ convex-combination idea, and this compares them on a model neither was
written for.

## The curve

GAMS discretises `sqrt(x)` into eight breakpoints: a straight line up to 50, six
sample points to 400, and a line out to 600 (the largest supply). It is chosen
to pass through the origin — so an unused route picks up no fixed cost — and to
underestimate `sqrt` everywhere in between.

| x | 0 | 50 | 120 | 190 | 260 | 330 | 400 | 600 |
|---|---|---|---|---|---|---|---|---|
| f(x) | 0 | 7.071 | 10.954 | 13.784 | 16.125 | 18.166 | 20 | 24.495 |

The [instance](https://github.com/FBumann/farkas/blob/main/examples/ports/data/transport_pwl.json) is otherwise
[Dantzig's](transport_dantzig.md), unchanged.

## The model

<!-- math:begin -->
<details markdown="1">
<summary>The same model, as math</summary>

#### Sets

| Symbol | Meaning |
|---|---|
| $\mathcal{P}$ | index $p$ --- `plant` |
| $\mathcal{M}$ | index $m$ --- `market` |
| $\mathcal{B}$ | index $b$ --- `bp` |

#### Parameters

| Symbol | Meaning |
|---|---|
| $\mathit{capacity}$ | `capacity` over $\mathcal{P}$ |
| $\mathit{demand}$ | `demand` over $\mathcal{M}$ |
| $\mathit{distance}$ | `distance` over $\mathcal{P} \times \mathcal{M}$ |
| $\mathit{freight}$ | `freight` (scalar) |
| $\mathit{bp}^{\mathrm{x}}$ | `bp_x` over $\mathcal{B}$ |
| $\mathit{bp}^{\mathrm{y}}$ | `bp_y` over $\mathcal{B}$ |

#### Variables

| Symbol | Meaning |
|---|---|
| $\mathit{shipment}$ | `shipment` over $\mathcal{P} \times \mathcal{M}$ |
| $\mathit{scaled}$ | `scaled` over $\mathcal{P} \times \mathcal{M}$ |
| $\mathit{economies\_of\_scale\_lam}$ | `economies_of_scale_lam` over $\mathcal{P} \times \mathcal{M} \times \mathcal{B}$ |
| $\mathit{economies\_of\_scale\_seg}$ | `economies_of_scale_seg` over $\mathcal{P} \times \mathcal{M} \times \mathcal{B}$ |

#### Objective

**`total_cost`**

$$\min \sum_{p \in \mathcal{P},\enspace m \in \mathcal{M}} \frac{\mathit{scaled}_{p,m} \cdot \mathit{distance}_{p,m} \cdot \mathit{freight}}{1000}$$

#### Subject to

**`within_capacity`**

$$\sum_{m \in \mathcal{M}} \mathit{shipment}_{p,m} \le \mathit{capacity}_{p} \qquad \forall\thinspace p \in \mathcal{P}$$

**`meet_demand`**

$$\sum_{p \in \mathcal{P}} \mathit{shipment}_{p,m} \ge \mathit{demand}_{m} \qquad \forall\thinspace m \in \mathcal{M}$$

**`economies_of_scale_convexity`**

$$\sum_{b \in \mathcal{B}} \mathit{economies\_of\_scale\_lam}_{p,m,b} = 1 \qquad \forall\thinspace p \in \mathcal{P},\enspace m \in \mathcal{M}$$

**`economies_of_scale_link0`**

$$\mathit{shipment}_{p,m} = \sum_{b \in \mathcal{B}} \mathit{economies\_of\_scale\_lam}_{p,m,b} \cdot \mathit{bp}^{\mathrm{x}}_{b} \qquad \forall\thinspace p \in \mathcal{P},\enspace m \in \mathcal{M}$$

**`economies_of_scale_link1`**

$$\mathit{scaled}_{p,m} = \sum_{b \in \mathcal{B}} \mathit{economies\_of\_scale\_lam}_{p,m,b} \cdot \mathit{bp}^{\mathrm{y}}_{b} \qquad \forall\thinspace p \in \mathcal{P},\enspace m \in \mathcal{M}$$

**`economies_of_scale_pick`**

$$\sum_{b \in \mathcal{B}} \mathit{economies\_of\_scale\_seg}_{p,m,b} = 1 \qquad \forall\thinspace p \in \mathcal{P},\enspace m \in \mathcal{M}$$

**`economies_of_scale_adjacency`**

$$\mathit{economies\_of\_scale\_lam}_{p,m,b} \le \mathit{economies\_of\_scale\_seg}_{p,m,b} + \mathit{economies\_of\_scale\_seg}_{p,m,b - 1} \qquad \forall\thinspace p \in \mathcal{P},\enspace m \in \mathcal{M},\enspace b \in \mathcal{B}$$

#### Variable domains

**`shipment`**

$$\mathit{shipment}_{p,m} \ge 0 \qquad \forall\thinspace p \in \mathcal{P},\enspace m \in \mathcal{M}$$

**`scaled`**

$$\mathit{scaled}_{p,m} \ge 0 \qquad \forall\thinspace p \in \mathcal{P},\enspace m \in \mathcal{M}$$

**`economies_of_scale_lam`**

$$0 \le \mathit{economies\_of\_scale\_lam}_{p,m,b} \le 1 \qquad \forall\thinspace p \in \mathcal{P},\enspace m \in \mathcal{M},\enspace b \in \mathcal{B}$$

**`economies_of_scale_seg`**

$$\mathit{economies\_of\_scale\_seg}_{p,m,b} \in \{0, 1\} \qquad \forall\thinspace p \in \mathcal{P},\enspace m \in \mathcal{M},\enspace b \in \mathcal{B}$$

</details>
<!-- math:end -->

```yaml
# Dantzig's transportation problem with economies of scale — GAMS model
# library `trnspwl`. Shipping cost grows as sqrt(x) rather than linearly, so a
# big consignment is cheaper per unit. Optimum 8.786852757777865, from linopy's
# own piecewise formulation. See docs/models/index.md.

dimensions:
  plant:
    values: [seattle, san-diego]
  market:
    values: [new-york, chicago, topeka]
  bp:
    dtype: int  # breakpoints of the discretised sqrt curve

parameters:
  capacity:
    dims: [plant]
  demand:
    dims: [market]
  distance:
    dims: [plant, market]
  freight:
    dims: []
  # The curve is the same on every route, so it carries `bp` alone and
  # broadcasts across (plant, market).
  bp_x:
    dims: [bp]
  bp_y:
    dims: [bp]

variables:
  shipment:
    foreach: [plant, market]
    bounds:
      lower: 0
  # What the objective is charged on: sqrt(shipment), read off the curve
  # rather than computed. `sqrt` is not sayable in the language and does not
  # need to be — the discretisation is the model GAMS publishes.
  scaled:
    foreach: [plant, market]
    bounds:
      lower: 0

piecewise:
  economies_of_scale:
    over: bp
    links:
      - [shipment, bp_x]
      - [scaled, bp_y]
    # Deliberately *not* `convex: true`. sqrt is concave and this is a
    # minimisation, so the convex-hull relaxation would let the solver ride the
    # chord underneath the true curve and buy transport cheaper than the model
    # allows. Segment binaries are what make the answer right — and what make
    # this port a MILP.

constraints:
  within_capacity:
    foreach: [plant]
    expression: sum(shipment, over=market) <= capacity
  meet_demand:
    foreach: [market]
    expression: sum(shipment, over=plant) >= demand

objectives:
  total_cost:
    sense: minimize
    expression: scaled * distance * freight / 1000
```

**`convex: true` would be wrong here, and quietly so.** `sqrt` is concave and
this is a minimisation, so the convex-hull relaxation lets the solver ride the
chord *underneath* the true curve and buy transport cheaper than the model
allows. Leaving it off emits segment binaries and an adjacency row, which is
what makes the answer right — and what makes this port a MILP rather than an LP.

That is the one judgement a reader has to make when writing a `piecewise:`
block, and it is not one the language can make for you: the curvature guard
catches *mixed* curvature, but a consistently concave curve under minimisation
is a modelling error, not a data error.

## Side by side

<details>
<summary><b>linopy</b> — <code>examples/ports/references/transport_pwl.py</code></summary>

```python
from __future__ import annotations

import json
from pathlib import Path

import linopy
import pandas as pd

DATA = Path(__file__).resolve().parent.parent / 'data' / 'transport_pwl.json'


def build(data: dict) -> linopy.Model:
    """The port's tables as a linopy model, column for column."""
    plants = pd.Index(data['plant']['plant'], name='plant')
    markets = pd.Index(data['market']['market'], name='market')

    capacity = pd.Series(data['capacity']['value'], index=plants)
    demand = pd.Series(data['demand']['value'], index=markets)
    distance = (
        pd.DataFrame(data['distance'])
        .pivot(index='plant', columns='market', values='value')
        .reindex(index=plants)[markets]
    )
    cost = distance * data['freight'] / 1000

    m = linopy.Model()
    shipment = m.add_variables(lower=0, coords=[plants, markets], name='shipment')
    # what the objective is actually charged on: sqrt(shipment), read off the
    # discretised curve rather than computed
    scaled = m.add_variables(lower=0, coords=[plants, markets], name='scaled')

    m.add_piecewise_formulation(
        (shipment, data['bp_x']['value']),
        (scaled, data['bp_y']['value']),
    )

    m.add_constraints(shipment.sum('market') <= capacity, name='within_capacity')
    m.add_constraints(shipment.sum('plant') >= demand, name='meet_demand')
    m.add_objective((scaled * cost).sum())
    return m


def main() -> float:
    m = build(json.loads(DATA.read_text()))
    status, condition = m.solve(solver_name='highs')
    assert status == 'ok', f'{status}: {condition}'
    print(f'linopy {linopy.__version__}')
    print(f'objective {float(m.objective.value)!r}')
    print(m.solution['shipment'].to_series())
    return float(m.objective.value)


if __name__ == '__main__':
    main()
```

</details>

## What it exercises

`piecewise:` on its non-convex path — segment binaries, the adjacency row, and
the integrality that follows — plus `sum` and parameter arithmetic in the
objective.

**It is also the first port whose numbers are not bit-identical.** farkas
returns `8.786852757777858` against linopy's `8.786852757777865`: a relative
difference of about 8 × 10⁻¹⁶, which is branch-and-bound arriving at the same
vertex by a different order of floating-point operations. The shipment plan is
identical. This is what per-port `rtol` is for, and why the corpus compares
objectives rather than bit patterns.
