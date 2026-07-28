#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pypsa==1.2.4", "linopy==0.9.0", "highspy==1.15.1"]
# # linopy is pinned because PyPSA builds its model *through* it: the
# # formulation, and so the number, is theirs jointly. Unpinned, a rerun
# # could resolve a different linopy and no longer be the run recorded.
# ///
"""Reference for ``pypsa_ramp``: PyPSA's own LOPF. See docs/ports.md.

    uv run --script examples/ports/references/pypsa_ramp.py

Pinned above to the versions that produced the number in ``references.json``,
and run out of band — PyPSA is not a dependency of this project.

It reads the same instance the port binds and builds the network with PyPSA's
own objects. Nothing here imports farkas.

Rung 2: rung 1 plus generator ramp limits. ``ramp_limit_up`` and
``ramp_limit_down`` are fractions of ``p_nom`` bounding the change between
consecutive snapshots, and PyPSA writes them from the *second* snapshot on —
there is no initial dispatch for the first to ramp from. That is the whole
delta; the network, the loads and the links are rung 1's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pypsa

DATA = Path(__file__).resolve().parent.parent / 'data' / 'pypsa_ramp.json'


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

    load = pd.DataFrame(data['load']).pivot(index='snapshot', columns='bus', values='value')
    for bus in data['bus']['bus']:
        n.add('Load', f'load_{bus}', bus=bus, p_set=load[bus])
    return n


def main() -> float:
    n = build(json.loads(DATA.read_text()))
    status, condition = n.optimize(solver_name='highs')
    # A ramp limit is the one rung that can make the instance infeasible rather
    # than merely different, and PyPSA reports that by leaving n.objective None
    # — which would otherwise surface as a TypeError three lines down.
    assert status == 'ok', f'{status}: {condition} — the ramp limits are tighter than the load swing'
    print(f'pypsa {pypsa.__version__}')
    print(f'objective {float(n.objective)!r}')
    print(n.generators_t.p)
    return float(n.objective)


if __name__ == '__main__':
    main()
