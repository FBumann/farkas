"""Guarded access to the compat/oracle lane.

Importing this module skips the importing test module when the ``[compat]``
extra is absent — ``pytest.importorskip`` raises ``Skipped``, and pytest turns
that into a skipped module at collection time.

Import the oracle *through here* rather than importing linopy or xarray
directly, so the guard cannot be bypassed by import ordering: isort sorts a
bare ``import xarray`` above a first-party import, and it would then blow up
as a collection error before any guard ran.

This replaces a hand-maintained list of filenames in ``conftest.py``, which
had to be edited every time a test module was added and silently mis-skipped
when it was not.
"""

from __future__ import annotations

import pandas as pd
import pytest

_REASON = 'needs the [compat] extra (linopy, xarray)'

linopy = pytest.importorskip('linopy', reason=_REASON)
xr = pytest.importorskip('xarray', reason=_REASON)

from linopy_yaml import builder, compat, loader  # noqa: E402  — must follow the guard above

__all__ = ['builder', 'compat', 'linopy', 'loader', 'transport_eager_objective', 'xr']


def transport_eager_objective(gens, lines, load) -> float:
    gi = gens.set_index('generator')
    li = lines.set_index('line')
    snapshots = pd.Index(sorted(load['snapshot'].unique()), name='snapshot')
    buses = pd.Index(sorted(load['bus'].unique()), name='bus')

    load_da = xr.DataArray.from_series(load.set_index(['snapshot', 'bus'])['value'])
    p_max = xr.DataArray.from_series(gi['p_max'])
    cost = xr.DataArray.from_series(gi['cost'])
    cap = xr.DataArray.from_series(li['cap'])

    gen_at = xr.DataArray(
        (gi['bus'].to_numpy()[None, :] == buses.to_numpy()[:, None]).astype(float),
        coords={'bus': buses, 'generator': gi.index},
        dims=['bus', 'generator'],
    )
    line_in = xr.DataArray(
        (li['to_bus'].to_numpy()[None, :] == buses.to_numpy()[:, None]).astype(float),
        coords={'bus': buses, 'line': li.index},
        dims=['bus', 'line'],
    )
    line_out = xr.DataArray(
        (li['from_bus'].to_numpy()[None, :] == buses.to_numpy()[:, None]).astype(float),
        coords={'bus': buses, 'line': li.index},
        dims=['bus', 'line'],
    )

    m = linopy.Model()
    p = m.add_variables(lower=0, upper=p_max, coords=[snapshots, gi.index], name='p')
    f = m.add_variables(lower=-cap, upper=cap, coords=[snapshots, li.index], name='f')
    injection = (p * gen_at).sum('generator') + (f * line_in).sum('line') - (f * line_out).sum('line')
    m.add_constraints(injection == load_da, name='balance')
    m.add_objective((p * cost).sum())
    m.solve(solver_name='highs', output_flag=False)
    return float(m.objective.value)
