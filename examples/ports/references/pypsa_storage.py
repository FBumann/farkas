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
"""Reference for ``pypsa_storage``: PyPSA's own LOPF. See docs/ports.md.

    uv run --script examples/ports/references/pypsa_storage.py

Pinned above to the versions that produced the number in ``references.json``,
and run out of band — PyPSA is not a dependency of this project.

It reads the same instance the port binds and builds the network with PyPSA's
own objects. Nothing here imports farkas.

Rung 3: rung 2 plus a ``StorageUnit``. Its state of charge carries energy
between snapshots, charged at ``efficiency_store`` and discharged at
``efficiency_dispatch``, decaying by ``standing_loss`` each step. Left
**non-cyclic** — the horizon starts at ``state_of_charge_initial`` and the end
is free — because closing that loop is rung 4 and should fail on its own.
"""

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
