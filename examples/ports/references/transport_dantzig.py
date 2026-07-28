#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["linopy==0.9.0", "pandas>=2.2", "highspy==1.15.1"]
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


def main() -> float:
    m = build(json.loads(DATA.read_text()))
    m.solve(solver_name='highs')
    print(f'linopy {linopy.__version__}')
    print(f'objective {float(m.objective.value)!r}')
    return float(m.objective.value)


if __name__ == '__main__':
    main()
