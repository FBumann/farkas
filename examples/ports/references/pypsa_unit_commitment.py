#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pypsa==1.2.4", "linopy==0.9.0", "pandas==3.0.5", "xarray==2026.7.0", "highspy==1.15.1"]
# # linopy is pinned because PyPSA builds its model *through* it: the
# # formulation, and so the number, is theirs jointly. pandas is pinned because
# # the recorded duals are reshaped with it, and `stack()` dropped NA rows by
# # default before 3.0 — an unpinned rerun could silently record a shorter
# # price vector than it solved for. xarray is linopy's data model, so
# # alignment and broadcasting decide which coefficient lands in which row.
# ///
"""Reference for ``pypsa_unit_commitment``: PyPSA's own UC. See docs/ports.md.

    uv run --script examples/ports/references/pypsa_unit_commitment.py

Pinned above to the versions that produced the number in ``references.json``,
and run out of band — PyPSA is not a dependency of this project.

It reads the same instance the port binds and builds the network with PyPSA's
own objects. Nothing here imports farkas.

**The MILP entry in the corpus.** ``committable=True`` gives each generator a
binary ``status`` per snapshot, plus binary ``start_up`` and ``shut_down``, and
that is the point: it is the first ported model with an integrality constraint.
One bus, no network — the ladder's lesson is that a rung which fails to match
should implicate one feature, and here that feature is commitment.

``min_up_time`` and ``min_down_time`` are left at 0. They would need a rolling
window sum over a horizon, which is a different question from whether the
language can say commitment at all, and it belongs to its own rung.
"""

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
