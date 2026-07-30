# Dantzig transport

Dantzig's transportation problem — GAMS model library #1, and the oldest LP in the corpus.

> **✔ Verified against published with GAMS model library #1 (trnsport)** — objective **153.675**, matched to `rtol=1e-09`.

## The problem

Ship from plants to markets at least cost, respecting capacity and meeting demand:

$$\min \sum_{i,j} c_{ij} x_{ij}
\quad\text{s.t.}\quad \sum_j x_{ij} \le a_i ,\quad \sum_i x_{ij} \ge b_j ,\quad x \ge 0$$

## The model

<!-- math:begin -->
<details markdown="1">
<summary>The same model, as math</summary>

#### Sets

| Symbol | Meaning |
|---|---|
| $\mathcal{I}$ | index $i$ --- `plant` --- canning plants, with limited capacity |
| $\mathcal{J}$ | index $j$ --- `market` --- markets, with demand to be met |

#### Parameters

| Symbol | Meaning |
|---|---|
| $a$ | `capacity` over $\mathcal{I}$ --- capacity of each plant |
| $b$ | `demand` over $\mathcal{J}$ --- demand at each market |
| $d$ | `distance` over $\mathcal{I} \times \mathcal{J}$ --- distance from plant to market |
| $f$ | `freight` (scalar) --- freight rate per case per unit distance |

#### Variables

| Symbol | Meaning |
|---|---|
| $x$ | `shipment` over $\mathcal{I} \times \mathcal{J}$ --- cases shipped from plant $i$ to market $j$ |

#### Objective

**`total_cost`**

$$\min \sum_{i \in \mathcal{I},\enspace j \in \mathcal{J}} \frac{x_{i,j} \cdot d_{i,j} \cdot f}{1000}$$

#### Subject to

**`within_capacity`**

$$\sum_{j \in \mathcal{J}} x_{i,j} \le a_{i} \qquad \forall\thinspace i \in \mathcal{I}$$

**`meet_demand`**

$$\sum_{i \in \mathcal{I}} x_{i,j} \ge b_{j} \qquad \forall\thinspace j \in \mathcal{J}$$

#### Variable domains

**`shipment`**

$$x_{i,j} \ge 0 \qquad \forall\thinspace i \in \mathcal{I},\enspace j \in \mathcal{J}$$

</details>
<!-- math:end -->

```yaml
# Dantzig's transportation problem (GAMS model library #1). Optimum 153.675,
# published with the model. See docs/models/index.md.

dimensions:
  plant:
    values: [seattle, san-diego]
  market:
    values: [new-york, chicago, topeka]

parameters:
  capacity:
    dims: [plant]
  demand:
    dims: [market]
  distance:
    dims: [plant, market]
  freight:
    dims: []

variables:
  shipment:
    foreach: [plant, market]
    bounds:
      lower: 0

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
  # c(i,j) = f * d(i,j) / 1000 in the source, kept as arithmetic here
  # rather than precomputed, so the file states the model and not a
  # derived table.
    expression: shipment * distance * freight / 1000
```

## Side by side

The same problem written by hand in linopy — a fair comparison, because linopy
is what a user of this project would otherwise reach for. Both formulations
solve to **153.675**; this script is run out of band and its number is recorded
in `references.json`.

<details>
<summary><b>linopy</b> — <code>examples/ports/references/transport_dantzig.py</code></summary>

```python
from __future__ import annotations

import json
from pathlib import Path

import linopy
import pandas as pd

DATA = Path(__file__).resolve().parent.parent / 'data' / 'transport_dantzig.json'


def build(data: dict) -> linopy.Model:
    """The port's tables as a linopy model, term for term."""
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
    m.add_constraints(shipment.sum('market') <= capacity, name='within_capacity')
    m.add_constraints(shipment.sum('plant') >= demand, name='meet_demand')
    m.add_objective((shipment * cost).sum())
    return m


def shadow_prices(m: linopy.Model, name: str, dim: str) -> dict[str, list]:
    """The dual of constraint *name*, tidy.

    Both of this model's constraints are *inequalities*, which is where sign
    conventions diverge most between implementations — a capacity's shadow
    price and a demand's carry opposite signs, and getting one backwards still
    produces a plausible-looking table. Recorded so the port is checked on
    them rather than only on the objective.
    """
    dual = m.constraints[name].dual
    return {dim: [str(v) for v in dual.indexes[dim]], 'value': [float(v) for v in dual.values]}


def main() -> float:
    m = build(json.loads(DATA.read_text()))
    status, condition = m.solve(solver_name='highs')
    # The other references assert this; without it a failed solve prints an
    # objective of whatever linopy left behind and a dual table read off a
    # solution that does not exist — recorded as fact in references.json.
    assert status == 'ok', f'{status}: {condition}'
    print(f'linopy {linopy.__version__}')
    print(f'objective {float(m.objective.value)!r}')
    print(
        f'duals {json.dumps({"within_capacity": shadow_prices(m, "within_capacity", "plant"), "meet_demand": shadow_prices(m, "meet_demand", "market")})}'
    )
    return float(m.objective.value)


if __name__ == '__main__':
    main()
```

</details>

The YAML above is 40 lines and names the maths; the linopy version is ~25 lines
of Python and names the *data structures* the maths is carried in — a pivot, a
reindex, two `.sum()` calls over named axes. Neither is obviously better and
that is the honest read: what the declarative form buys here is not brevity but
that the file is the model, with no host language between the reader and it.

## What it exercises

The freight rate is kept as arithmetic — `distance * freight / 1000` — rather
than precomputed into a cost table, so the file states the model and not a
derived table. `freight` is declared with `dims: []`: a scalar is a parameter
with no dimensions, not a special case.

The objective is checked, never the primal. This model reaches 153.675 at a
**different vertex** than the source prints, so a corpus pinned to a solution
would fail on a solver upgrade that broke nothing.

---

[`examples/ports/transport_dantzig.yaml`](https://github.com/FBumann/lpspec/blob/main/examples/ports/transport_dantzig.yaml) · back to [all models](index.md)
