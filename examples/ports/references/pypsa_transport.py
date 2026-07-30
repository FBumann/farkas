#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pypsa==1.2.4", "linopy==0.9.0", "pandas>=2.2", "xarray==2026.7.0", "highspy==1.15.1"]
# # linopy is pinned because PyPSA builds its model *through* it: the
# # formulation, and so the number, is theirs jointly. pandas is pinned because
# # xarray is linopy's data model, so alignment and broadcasting decide which
# # coefficient lands in which row. pandas is only a floor: it reshapes the
# # recorded duals and nothing else, and `nodal_prices` spells that reshape
# # out rather than leaning on `stack()`, whose NA handling changed in 3.0.
# # Checked: this script emits byte-identical output on pandas 2.3.3 and
# # 3.0.5, so the floor is a measured claim rather than an assumption.
# ///
"""Reference for ``pypsa_transport``: PyPSA's own LOPF. See docs/models/index.md.

    uv run --script examples/ports/references/pypsa_transport.py

Pinned above to the versions that produced the number in ``references.json``,
and run out of band — PyPSA is not a dependency of this project.

It reads the same instance the port binds, since a reference optimum means
nothing against a different one, and builds the network with PyPSA's own
objects. Nothing here imports lpspec.

Rung 1: transport model, linear marginal cost. Links rather than lines is what
makes it one — a link's flow is a variable bounded by its rating, with no
Kirchhoff voltage law. Hence efficiency 1.0, nothing extendable, no capital
cost, no snapshot weightings.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pypsa

DATA = Path(__file__).resolve().parent.parent / 'data' / 'pypsa_transport.json'


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
    # `p_min_pu = -1` makes a link bidirectional. The port cannot say that in a
    # bound — bounds take a name or a number, never arithmetic (SPEC §2) — so
    # it ships `neg_rating` as data instead. That is the ledger row.
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
    n.optimize(solver_name='highs')
    print(f'pypsa {pypsa.__version__}')
    print(f'objective {float(n.objective)!r}')
    print(f'duals {json.dumps({"nodal_balance": nodal_prices(n)})}')
    return float(n.objective)


if __name__ == '__main__':
    main()
