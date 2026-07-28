#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pypsa==1.2.4", "linopy==0.9.0", "highspy==1.15.1"]
# # linopy is pinned because PyPSA builds its model *through* it: the
# # formulation, and so the number, is theirs jointly. Unpinned, a rerun
# # could resolve a different linopy and no longer be the run recorded.
# ///
"""Reference for ``pypsa_cyclic_storage``: PyPSA's own LOPF. See docs/ports.md.

    uv run --script examples/ports/references/pypsa_cyclic_storage.py

Pinned above to the versions that produced the number in ``references.json``,
and run out of band — PyPSA is not a dependency of this project.

It reads the same instance the port binds and builds the network with PyPSA's
own objects. Nothing here imports farkas.

Rung 4: rung 3's storage with ``cyclic_state_of_charge``. The first
snapshot's state of charge carries over from the *last* rather than from a
seed, so the horizon closes on itself and there is no
``state_of_charge_initial``. That is the whole delta from rung 3.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pypsa

DATA = Path(__file__).resolve().parent.parent / 'data' / 'pypsa_cyclic_storage.json'


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
        efficiency_store=data['efficiency_store']['value'],
        efficiency_dispatch=data['efficiency_dispatch']['value'],
        standing_loss=data['standing_loss']['value'],
        cyclic_state_of_charge=True,
    )

    load = pd.DataFrame(data['load']).pivot(index='snapshot', columns='bus', values='value')
    for bus in data['bus']['bus']:
        n.add('Load', f'load_{bus}', bus=bus, p_set=load[bus])
    return n


def main() -> float:
    n = build(json.loads(DATA.read_text()))
    status, condition = n.optimize(solver_name='highs')
    assert status == 'ok', f'{status}: {condition}'
    print(f'pypsa {pypsa.__version__}')
    print(f'objective {float(n.objective)!r}')
    print(n.generators_t.p)
    print(n.storage_units_t.state_of_charge)
    return float(n.objective)


if __name__ == '__main__':
    main()
