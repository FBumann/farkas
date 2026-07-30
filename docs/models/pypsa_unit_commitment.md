# PyPSA unit commitment

Which generators are *on*, not just how much they produce — a binary per generator per snapshot, with start-up and shut-down charges.

> **✔ Verified against pypsa 1.2.4 (its own linopy 0.9.0)** — objective **24900**, matched to `rtol=1e-09`.

**The corpus's MILP entry.** Every other verified model is a pure continuous
LP; this one carries integrality, which is what the gallery's construct matrix
had no verified example of. One bus and no network, deliberately: a model that
fails to match should implicate one feature, and here that feature is
commitment.

`min_up_time` and `min_down_time` are left at 0. They need a rolling window sum
over a horizon, which is a different question from whether the language can say
commitment at all — see [the ledger](index.md#ledger--what-a-port-could-not-say).

## The model

<!-- math:begin -->
<details markdown="1">
<summary>The same model, as math</summary>

#### Sets

| Symbol | Meaning |
|---|---|
| $\mathcal{T}$ | index $t$ --- `snapshot` |
| $\mathcal{G}$ | index $g$ --- `generator` |

#### Parameters

| Symbol | Meaning |
|---|---|
| $p^{\mathrm{nom}}$ | `p_nom` over $\mathcal{G}$ |
| $\mathit{marginal\_cost}$ | `marginal_cost` over $\mathcal{G}$ |
| $p^{\mathrm{min,pu}}$ | `p_min_pu` over $\mathcal{G}$ |
| $\mathit{start\_up\_cost}$ | `start_up_cost` over $\mathcal{G}$ |
| $\mathit{shut\_down\_cost}$ | `shut_down_cost` over $\mathcal{G}$ |
| $\mathit{load}$ | `load` over $\mathcal{T}$ |

#### Variables

| Symbol | Meaning |
|---|---|
| $p$ | `p` over $\mathcal{T} \times \mathcal{G}$ |
| $\mathit{status}$ | `status` over $\mathcal{T} \times \mathcal{G}$ |
| $\mathit{start\_up}$ | `start_up` over $\mathcal{T} \times \mathcal{G}$ |
| $\mathit{shut\_down}$ | `shut_down` over $\mathcal{T} \times \mathcal{G}$ |

$t \ominus k$ denotes cyclic translation: index $t-k$ taken modulo the size of the dimension (`roll`). Plain $t-k$ (`shift`) has no wraparound --- terms translated past the edge are simply absent.

#### Objective

**`total_cost`**

$$\min \sum_{t \in \mathcal{T},\enspace g \in \mathcal{G}} \left( p_{t,g} \cdot \mathit{marginal\_cost}_{g} + \mathit{start\_up}_{t,g} \cdot \mathit{start\_up\_cost}_{g} + \mathit{shut\_down}_{t,g} \cdot \mathit{shut\_down\_cost}_{g} \right)$$

#### Subject to

**`power_balance`**

$$\sum_{g \in \mathcal{G}} p_{t,g} = \mathit{load}_{t} \qquad \forall\thinspace t \in \mathcal{T}$$

**`commitment_max`**

$$p_{t,g} - p^{\mathrm{nom}}_{g} \cdot \mathit{status}_{t,g} \le 0 \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G}$$

**`commitment_min`**

$$p_{t,g} - p^{\mathrm{min,pu}}_{g} \cdot p^{\mathrm{nom}}_{g} \cdot \mathit{status}_{t,g} \ge 0 \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G}$$

**`start_up_initial`**

$$\mathit{start\_up}_{t,g} - \mathit{status}_{t,g} \ge -1 \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G} \thinspace:\thinspace t = 0$$

**`start_up`**

$$\mathit{start\_up}_{t,g} - \mathit{status}_{t,g} + \mathit{status}_{t \ominus 1,g} \ge 0 \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G} \thinspace:\thinspace t > 0$$

**`shut_down_initial`**

$$\mathit{shut\_down}_{t,g} + \mathit{status}_{t,g} \ge 1 \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G} \thinspace:\thinspace t = 0$$

**`shut_down`**

$$\mathit{shut\_down}_{t,g} + \mathit{status}_{t,g} - \mathit{status}_{t \ominus 1,g} \ge 0 \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G} \thinspace:\thinspace t > 0$$

#### Variable domains

**`p`**

$$p_{t,g} \ge 0 \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G}$$

**`status`**

$$\mathit{status}_{t,g} \in \{0, 1\} \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G}$$

**`start_up`**

$$\mathit{start\_up}_{t,g} \in \{0, 1\} \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G}$$

**`shut_down`**

$$\mathit{shut\_down}_{t,g} \in \{0, 1\} \qquad \forall\thinspace t \in \mathcal{T},\enspace g \in \mathcal{G}$$

</details>
<!-- math:end -->

```yaml
# PyPSA unit commitment: binary status per generator per snapshot, with
# start-up and shut-down charges. Optimum 24900.0, from PyPSA itself.
# See docs/models/index.md.

dimensions:
  snapshot:
    dtype: int
  generator:
    dtype: str

parameters:
  p_nom:
    dims: [generator]
  marginal_cost:
    dims: [generator]
  p_min_pu:
    dims: [generator]
  start_up_cost:
    dims: [generator]
  shut_down_cost:
    dims: [generator]
  load:
    dims: [snapshot]

variables:
  p:
    foreach: [snapshot, generator]
    bounds:
      lower: 0
  # All three are binary in PyPSA. start_up and shut_down are implied by the
  # transitions below, but PyPSA declares them integral rather than leaving it
  # to the status variables, and the port matches that.
  status:
    foreach: [snapshot, generator]
    binary: true
  start_up:
    foreach: [snapshot, generator]
    binary: true
  shut_down:
    foreach: [snapshot, generator]
    binary: true

constraints:
  power_balance:
    foreach: [snapshot]
    expression: sum(p, over=generator) == load

  # A committed unit runs between p_min_pu * p_nom and p_nom; an uncommitted
  # one is pinned to zero from both sides. `p_nom * status` is a parameter
  # against a variable, so the product stays degree 1.
  commitment_max:
    foreach: [snapshot, generator]
    expression: p - p_nom * status <= 0

  commitment_min:
    foreach: [snapshot, generator]
    expression: p - p_min_pu * p_nom * status >= 0

  # start_up must be 1 on a snapshot where status rises, shut_down where it
  # falls. The first snapshot has no predecessor, and PyPSA's default is that
  # the unit was already up before the horizon — so the start-up row is
  # slackened to -1 there (never binding) while the shut-down row still
  # charges a unit that begins the horizon off. That asymmetry is PyPSA's, and
  # it is worth 50 on this instance.
  start_up_initial:
    foreach: [snapshot, generator]
    where: "snapshot == 0"
    expression: start_up - status >= -1

  start_up:
    foreach: [snapshot, generator]
    where: "snapshot > 0"
    expression: start_up - status + roll(status, snapshot=1) >= 0

  shut_down_initial:
    foreach: [snapshot, generator]
    where: "snapshot == 0"
    expression: shut_down + status >= 1

  shut_down:
    foreach: [snapshot, generator]
    where: "snapshot > 0"
    expression: shut_down + status - roll(status, snapshot=1) >= 0

objectives:
  total_cost:
    sense: minimize
    expression: p * marginal_cost + start_up * start_up_cost + shut_down * shut_down_cost
```

**The first snapshot is not like the others.** PyPSA's default is that a unit
was already up before the horizon began, so the start-up row is slackened to
`>= -1` there and never binds, while the shut-down row still charges a unit
that begins the horizon *off*. `peak` does, so the instance pays a shut-down it
never visibly performs. That asymmetry is PyPSA's, it is worth 50 here, and
reproducing it is most of what makes this a fidelity test rather than a
plausible-looking rewrite.

Two `where` clauses on one constraint block is how the language says "this row
differs at the boundary" — the same shape [storage](storage.md) uses for its
initial state of charge.

## What it costs

| | |
|---|---|
| energy | `30 × 520 + 90 × 100` = 24600 |
| start-ups | `peak` at snapshot 1 = 200 |
| shut-downs | `peak` at snapshot 0 (begins off) and at 3 = 100 |
| **total** | **24900** |

`base` runs throughout; `peak` covers the two peak snapshots. lpspec and PyPSA
agree on the schedule as well as the cost.

## Side by side

<details>
<summary><b>PyPSA</b> — <code>examples/ports/references/pypsa_unit_commitment.py</code></summary>

```python
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pypsa

DATA = Path(__file__).resolve().parent.parent / 'data' / 'pypsa_unit_commitment.json'


def build(data: dict[str, dict[str, list]]) -> pypsa.Network:
    """The port's tables as a PyPSA network, column for column."""
    n = pypsa.Network()
    n.set_snapshots(data['snapshot']['snapshot'])
    n.add('Bus', 'bus')

    n.add(
        'Generator',
        data['generator']['generator'],
        bus='bus',
        committable=True,
        p_nom=data['p_nom']['value'],
        marginal_cost=data['marginal_cost']['value'],
        p_min_pu=data['p_min_pu']['value'],
        start_up_cost=data['start_up_cost']['value'],
        shut_down_cost=data['shut_down_cost']['value'],
    )

    load = pd.Series(data['load']['value'], index=data['load']['snapshot'])
    n.add('Load', 'load', bus='bus', p_set=load)
    return n


def main() -> float:
    n = build(json.loads(DATA.read_text()))
    status, condition = n.optimize(solver_name='highs')
    assert status == 'ok', f'{status}: {condition}'
    print(f'pypsa {pypsa.__version__}')
    print(f'objective {float(n.objective)!r}')
    print(n.generators_t.p)
    print(n.generators_t.status)
    return float(n.objective)


if __name__ == '__main__':
    main()
```

</details>

## What it exercises

`binary` variables and the integrality path through to HiGHS, `roll` across a
boundary condition, and a three-term objective mixing an energy cost with two
transition charges.
