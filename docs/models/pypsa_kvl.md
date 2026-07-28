# PyPSA LOPF — rung 5, Kirchhoff's voltage law

Passive AC lines: flow is decided by physics, not chosen. **The last rung of the ladder.**

> **✔ Verified against pypsa 1.2.4 (its own linopy 0.9.0)** — objective **17000**, matched to `rtol=1e-09`, nodal prices and line flows included.

Every earlier rung moved power over `Link` objects, whose flow is a decision
variable — a transport model. A `Line` is passive: around every independent
cycle of the network, the reactance-weighted flows must sum to zero.

It builds on [rung 1](pypsa_transport.md) rather than on
[rung 4](pypsa_cyclic_storage.md), and that is deliberate. Rungs 2–4 are
**time**-coupling — ramps, state of charge, a closed horizon. This one is
**space**-coupling. The two axes are independent, so stacking them would only
make a mismatch ambiguous about which caused it.

Three buses in a triangle, so the cycle space has exactly one dimension. The
flows come out fractional — 46.67 / 26.67 / −33.33 at the first snapshot —
because the split is forced by reactance rather than chosen by cost, which is
the whole difference between a line and a link.

## The model

<!-- math:begin -->
<details>
<summary>The same model, as math</summary>

#### Sets

| Symbol | Meaning |
|---|---|
| $\mathcal{T}$ | index $t$ --- `snapshot` |
| $\mathcal{B}$ | index $b$ --- `bus` |
| $\mathcal{G}$ | index $g$ --- `generator` with $\mathrm{bus}: \mathcal{G} \to \mathcal{B}$ |
| $\mathcal{L}$ | index $l$ --- `line` with $\mathrm{from}: \mathcal{L} \to \mathcal{B},\ \mathrm{to}: \mathcal{L} \to \mathcal{B}$ |
| $\mathcal{C}$ | index $c$ --- `cycle` |

#### Parameters

| Symbol | Meaning |
|---|---|
| $p^{\mathrm{nom}}$ | `p_nom` over $\mathcal{G}$ |
| $\mathit{marginal\_cost}$ | `marginal_cost` over $\mathcal{G}$ |
| $s^{\mathrm{nom}}$ | `s_nom` over $\mathcal{L}$ |
| $\mathit{neg\_s\_nom}$ | `neg_s_nom` over $\mathcal{L}$ |
| $\mathit{cycle}^{\mathrm{incidence}}$ | `cycle_incidence` over $\mathcal{C} \times \mathcal{L}$ |
| $\mathit{load}$ | `load` over $\mathcal{T} \times \mathcal{B}$ |

#### Variables

| Symbol | Meaning |
|---|---|
| $p$ | `p` over $\mathcal{T} \times \mathcal{G}$ |
| $f$ | `f` over $\mathcal{T} \times \mathcal{L}$ |

#### Objective

$$\begin{aligned}
\text{total\_cost:} \quad & \min & \sum_{t \in \mathcal{T},\ g \in \mathcal{G}} p_{t,g} \cdot \mathit{marginal\_cost}_{g}
\end{aligned}$$

#### Subject to

$$\begin{aligned}
\text{nodal\_balance:} \quad & \sum_{g \in \mathcal{G} \,:\, \mathrm{bus}(g) = b} p_{t,g} + \sum_{l \in \mathcal{L} \,:\, \mathrm{to}(l) = b} f_{t,l} - \left( \sum_{l \in \mathcal{L} \,:\, \mathrm{from}(l) = b} f_{t,l} \right) & = \mathit{load}_{t,b} && \forall\, t \in \mathcal{T},\ b \in \mathcal{B} \\
\text{kirchhoff\_voltage\_law:} \quad & \sum_{l \in \mathcal{L}} f_{t,l} \cdot \mathit{cycle}^{\mathrm{incidence}}_{c,l} & = 0 && \forall\, t \in \mathcal{T},\ c \in \mathcal{C}
\end{aligned}$$

#### Variable domains

$$\begin{aligned}
\text{p:} \quad & 0 \le p_{t,g} & \le p^{\mathrm{nom}}_{g} && \forall\, t \in \mathcal{T},\ g \in \mathcal{G} \\
\text{f:} \quad & \mathit{neg\_s\_nom}_{l} \le f_{t,l} & \le s^{\mathrm{nom}}_{l} && \forall\, t \in \mathcal{T},\ l \in \mathcal{L}
\end{aligned}$$

</details>
<!-- math:end -->

```yaml
# PyPSA linear optimal power flow, rung 5: passive AC lines under Kirchhoff's
# voltage law, rather than links whose flow is chosen.
# Optimum 17000.0, from PyPSA itself. See docs/ports.md.

dimensions:
  snapshot:
    dtype: int
  bus:
    dtype: str
  generator:
    dtype: str
    coords: [bus]  # every generator sits on a bus
  line:
    dtype: str
    coords: {from: bus, to: bus}  # both endpoints are buses
  cycle:
    dtype: str  # one independent loop of the network

parameters:
  p_nom:
    dims: [generator]
  marginal_cost:
    dims: [generator]
  s_nom:
    dims: [line]
  neg_s_nom:
    dims: [line]
  # The cycle basis, as a sparse (cycle, line) table of reactance x direction.
  # A line may belong to several cycles, so this cannot be a coordinate on
  # `line` — a coordinate is single-valued per label. It is a parameter over
  # both dims, and rows are absent where a line is not in a cycle.
  cycle_incidence:
    dims: [cycle, line]
  load:
    dims: [snapshot, bus]

variables:
  p:
    foreach: [snapshot, generator]
    bounds:
      lower: 0
      upper: p_nom
  # A line's flow is not chosen: it is whatever the voltage law leaves.
  f:
    foreach: [snapshot, line]
    bounds:
      lower: neg_s_nom
      upper: s_nom

constraints:
  nodal_balance:
    foreach: [snapshot, bus]
    equations:
      - expression: >-
          group_sum(p, over=generator, by=bus)
          + group_sum(f, over=line, by=to)
          - group_sum(f, over=line, by=from)
          == load

  # Kirchhoff's voltage law: around each independent cycle, the
  # reactance-weighted flows sum to zero. The incidence table carries both
  # which lines are in the cycle and which way round they run, so this is one
  # equation rather than a case analysis over the topology.
  kirchhoff_voltage_law:
    foreach: [snapshot, cycle]
    equations:
      - expression: sum(f * cycle_incidence, over=line) == 0

objectives:
  total_cost:
    sense: minimize
    equations:
      - expression: p * marginal_cost
```

**The cycle basis is a parameter, not a coordinate.** This is the one shape
decision worth reading twice. A line may belong to *several* cycles, and a
declared coordinate is single-valued per label — so `cycle_incidence` is a
parameter over `(cycle, line)` carrying reactance × direction, with rows simply
absent where a line is not in a cycle. Row absence is how this language spells
sparsity everywhere else, and a cycle-line incidence matrix is exactly the
sparse thing it is good at.

That makes the constraint one equation, `sum(f * cycle_incidence, over=line) ==
0`, rather than a case analysis over the topology — and it keeps
[topology as data](pypsa_transport.md): a fourth bus is more rows, not a
different file.

**Computing the basis is data preparation, and stays outside the language.**
Finding a cycle basis is a graph algorithm — iteration over a structure
discovered from data, which the
[ceiling](../ARCHITECTURE.md#two-tiers-and-the-ceiling) refuses by design. The
reference prints the rows PyPSA derived so the two can be checked against each
other. They need only agree on the *cycle space*: PyPSA scales its coefficients
for conditioning, and since the row is `= 0`, any nonzero multiple of a cycle
says the same thing.

## Side by side

```python
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pypsa

DATA = Path(__file__).resolve().parent.parent / 'data' / 'pypsa_kvl.json'


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
    )
    # r=0 keeps the line purely reactive: the linearised power flow is a
    # function of x alone, and a resistance would only add losses the DC
    # approximation does not model anyway.
    n.add(
        'Line',
        data['line']['line'],
        bus0=data['line']['from'],
        bus1=data['line']['to'],
        x=data['reactance']['value'],
        r=0.0,
        s_nom=data['s_nom']['value'],
    )

    load = pd.DataFrame(data['load']).pivot(index='snapshot', columns='bus', values='value')
    for bus in data['bus']['bus']:
        n.add('Load', f'load_{bus}', bus=bus, p_set=load[bus])
    return n


def nodal_prices(n: pypsa.Network) -> dict[str, list]:
    """PyPSA's marginal price per (snapshot, bus), tidy."""
    mp = n.buses_t.marginal_price
    return {
        'snapshot': [s for s in mp.index for _ in mp.columns],
        'bus': [b for _ in mp.index for b in mp.columns],
        'value': [float(v) for row in mp.to_numpy() for v in row],
    }


def cycle_basis(n: pypsa.Network) -> str:
    """The KVL rows PyPSA built, so the port's incidence can be checked by eye.

    PyPSA scales the coefficients for conditioning; the constraint is ``= 0``,
    so any nonzero multiple of a cycle describes the same cycle space. What has
    to match is which lines share a row and with what relative signs.
    """
    return str(n.model.constraints['Kirchhoff-Voltage-Law'])


def main() -> float:
    n = build(json.loads(DATA.read_text()))
    n.optimize.create_model(include_objective_constant=False)
    print(cycle_basis(n))
    status, condition = n.optimize(solver_name='highs')
    assert status == 'ok', f'{status}: {condition}'
    print(f'pypsa {pypsa.__version__}')
    print(f'objective {float(n.objective)!r}')
    print(f'duals {json.dumps({"nodal_balance": nodal_prices(n)})}')
    print(n.lines_t.p0)
    return float(n.objective)


if __name__ == '__main__':
    main()
```

## What it exercises

A parameter over two dimensions multiplying a variable over one, reduced along
the shared dimension — the shape that makes an incidence matrix sayable at all.
Plus `group_sum` on both line endpoints for the nodal balance, as in rung 1.

No new construct was needed for the last rung of the ladder, which is the
result worth reporting.
