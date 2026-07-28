# Ported models

Models somebody else already solved, said again in this language, and checked
against **an optimum that did not come from us**.

Every other test compares farkas against farkas. Even the differential harness
compares two lanes consuming the *same resolved AST*
([hard rule 1](../ARCHITECTURE.md#hard-rules)) — which is what makes them an
oracle for each other, and also what they cannot see: a **shared misreading**,
both lanes agreeing on a meaning the modeller did not intend, passes the whole
suite green. This is the net for that class, and the evidence behind
[the ceiling](../ARCHITECTURE.md#two-tiers-and-the-ceiling).

| Port | Reference | Optimum |
|---|---|---|
| [Dantzig transport](#dantzigs-transportation-problem) | published, GAMS model library #1 | 153.675 |
| [PyPSA LOPF rung 1](#pypsa-lopf--rung-1) | PyPSA 1.2.4, its own linopy 0.9.0 | 22000.0 |

## How a port is put together

`<name>.yaml` is the model, `data/<name>.json` the instance,
`references/<name>.py` a reference implementation importing no farkas, and
`references.json` the recorded objective with its provenance.

Reference scripts are **never run by CI** — pinning PyPSA into this project
would hand their release cadence a veto over the suite. They carry their deps
inline ([PEP 723](https://peps.python.org/pep-0723/)), pinned to what produced
the recorded number, so the corpus itself needs no oracle and no extra
dependency and runs on the bare-install job:

```
uv run --script examples/ports/references/pypsa_transport.py
```

A port whose optimum is *published* needs no script at all.

The instance is **data both sides read** — a reference optimum against a
different instance means nothing. What stays independent is the formulation.
`rtol` is per port, since a published optimum is rounded and a solved one is
not.

## The ladder

Reproducing a full PyPSA objective means reproducing marginal *and* capital
cost, ramp limits, storage cycling and KVL at once, and a mismatch then
implicates five features instead of one. So each network is a ladder, one
feature per rung, each switched off in PyPSA and reproduced here: **1 transport
model** (below) · 2 ramp limits · 3 storage with state of charge · 4 cyclic
boundary condition · 5 KVL.

A rung that matches is a row in the table above; one that **cannot be said** is
a row in the ledger. Both are evidence, so no rung is wasted.

## Ledger — what a port could not say

Feeds [ROADMAP.md](../ROADMAP.md), with the verdict
[CLAUDE.md](../CLAUDE.md) asks for: macro, primitive, or escape.

| Port | What could not be said | Worked around by | Verdict |
|---|---|---|---|
| PyPSA rung 1 | a bound of `-rating` — PyPSA's `p_min_pu = -1` | shipping `neg_rating` as data | **primitive**: bounds as expressions, [#31](https://github.com/FBumann/farkas/issues/31). A second model asking for it |

One row from two ports — a rate worth watching once the corpus has hit the
ceiling a few more times.

---

## Dantzig's transportation problem

GAMS model library #1 (`trnsport`), after Dantzig, *Linear Programming and
Extensions* (1963) ch. 3.3. **Optimum 153.675, published with the model.**

Plants $i$ have capacity $a_i$, markets $j$ demand $b_j$, $d_{ij}$ is the
distance in thousands of miles, and with freight rate $f$ the unit cost is
$c_{ij} = f \cdot d_{ij}/1000$.

$$\min_{x \ge 0} \sum_{i,j} c_{ij} x_{ij} \qquad \sum_j x_{ij} \le a_i \quad \sum_i x_{ij} \ge b_j$$

| | new-york | chicago | topeka | **capacity** |
|---|---|---|---|---|
| **seattle** | 2.5 | 1.7 | 1.8 | 350 |
| **san-diego** | 2.5 | 1.8 | 1.4 | 600 |
| **demand** | 325 | 300 | 275 | |

Supply totals 950 against demand of 900, so capacity is `<=`.

```yaml
# Dantzig's transportation problem (GAMS model library #1). Optimum 153.675,
# published with the model. See docs/ports.md.

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
    equations:
      - expression: sum(shipment, over=market) <= capacity
  meet_demand:
    foreach: [market]
    equations:
      - expression: sum(shipment, over=plant) >= demand

objectives:
  total_cost:
    sense: minimize
    equations:
      # c(i,j) = f * d(i,j) / 1000 in the source, kept as arithmetic here
      # rather than precomputed, so the file states the model and not a
      # derived table.
      - expression: shipment * distance * freight / 1000
```

**The cost table is not in the data.** The source defines $c_{ij}$ as derived
and the objective writes it that way — one variable against two parameters and
a constant, still degree 1.

**It finds a different optimum than the source prints** — 300 seattle→chicago
and 325 san-diego→new-york, against the source's 50 and 275 into new-york. Both
cost 153.675. Alternative optima, on the first port, which is why the corpus
asserts objectives and never primals.

## PyPSA LOPF — rung 1

[PyPSA](https://github.com/PyPSA/PyPSA) 1.2.4, solved by
[`references/pypsa_transport.py`](../examples/ports/references/pypsa_transport.py).
**Optimum 22000.0.** PyPSA is where this package's audience comes from, so it
is the port that matters most.

Rung 1 uses **links, not lines**: a link's flow is a variable bounded by its
rating, with no Kirchhoff voltage law — exactly a transport model, and the
subset of LOPF that needs no new language.

$$\min \sum_{t,g} o_g\, p_{t,g} \qquad 0 \le p_{t,g} \le P^{\text{nom}}_g \qquad -F_\ell \le f_{t,\ell} \le F_\ell$$

$$\sum_{g:\,\text{bus}(g)=b} p_{t,g} \;+\!\!\sum_{\ell:\,\text{to}(\ell)=b}\!\! f_{t,\ell} \;-\!\!\sum_{\ell:\,\text{from}(\ell)=b}\!\! f_{t,\ell} \;=\; d_{t,b}$$

Three buses in a line, four snapshots: wind at north (100 MW, cost 0), gas at
mid (150, 50), oil at south (80, 80); links north→mid rated 60, mid→south 50.
Load is set so the cheap generator is capped by **link ratings** rather than its
own $P^{\text{nom}}$ — otherwise the network is decorative. Both links run
saturated at the optimum, which is the check that it worked.

```yaml
# PyPSA linear optimal power flow, rung 1: transport model, linear marginal
# cost, no KVL. Optimum 22000.0, from PyPSA itself. See docs/ports.md.

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

parameters:
  p_nom:
    dims: [generator]
  marginal_cost:
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
  # PyPSA's `p0`: flow measured at the link's `from` end, so a positive value
  # withdraws there and injects at `to`. `p_min_pu = -1` makes it bidirectional.
  f:
    foreach: [snapshot, link]
    bounds:
      lower: neg_rating
      upper: rating

constraints:
  nodal_balance:
    foreach: [snapshot, bus]
    equations:
      - expression: group_sum(p, over=generator, by=bus) + group_sum(f, over=link, by=to) - group_sum(f, over=link, by=from) == load

objectives:
  total_cost:
    sense: minimize
    equations:
      - expression: p * marginal_cost
```

**Topology is data.** `generator` declares a `bus` coordinate, `link` declares
`from` and `to`, both arriving as index tables — adding a bus is a row, not an
edit to the model.

**`group_sum` is what makes the balance sayable**: each term projects a
different dimension onto `bus`, three joins against the dim tables, still
pointwise.

The dispatch agrees with PyPSA's generator for generator. The corpus still
asserts only the objective, for the reason rung 0 ran into.
