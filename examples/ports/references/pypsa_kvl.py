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
"""Reference for ``pypsa_kvl``: PyPSA's own LOPF with lines. See docs/ports.md.

    uv run --script examples/ports/references/pypsa_kvl.py

Pinned above to the versions that produced the number in ``references.json``,
and run out of band — PyPSA is not a dependency of this project.

It reads the same instance the port binds and builds the network with PyPSA's
own objects. Nothing here imports farkas.

**Rung 5, the last one: Kirchhoff's voltage law.** Every earlier rung moved
power over ``Link`` objects, whose flow is a decision variable — a transport
model. A ``Line`` is passive: flow is decided by physics, and around every
independent cycle the reactance-weighted flows must sum to zero. That is what
makes this the network-physics rung rather than another time-coupling one, and
why it builds on rung 1 rather than on rung 4: the two axes are independent,
and mixing them would leave a mismatch ambiguous.

It also prints the cycle basis PyPSA derived, because the port carries that
basis as data (``cycle_incidence``) and the two must describe the same cycle
space. Computing a cycle basis is a graph algorithm, which is data preparation
and deliberately outside the language.
"""

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
    mp = n.buses_t.marginal_price.stack().reset_index()
    mp.columns = ['snapshot', 'bus', 'value']
    return {c: mp[c].tolist() for c in mp.columns}


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
