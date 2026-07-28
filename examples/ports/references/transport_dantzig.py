#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["linopy==0.9.0", "pandas==3.0.5", "xarray==2026.7.0", "highspy==1.15.1"]
# ///
"""Reference for ``transport_dantzig``: the same LP, hand-written in linopy.

    uv run --script examples/ports/references/transport_dantzig.py

**This is not what verifies the port.** The optimum came from the literature —
published with GAMS model library #1 — and that is what ``references.json``
records as primary. This script is a *second, independent* arrival at the same
number, from a different formulation in a different tool, and it exists for two
reasons:

- a published constant proves the answer, not that anybody can still get it;
- the docs put this file side by side with the YAML to let a reader judge
  readability, and code shown next to a claim about legibility has to be code
  that runs. An unexecuted script in a docs page rots silently, which is the
  failure the whole ports corpus exists to avoid.

Run out of band with the pinned versions above, like every reference here:
linopy is not a runtime dependency of this project. Nothing here imports
farkas.
"""

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
