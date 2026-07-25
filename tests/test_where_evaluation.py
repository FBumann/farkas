"""Eager reading of a where AST — ``builder.evaluate_where``.

Split out of test_parser.py when the evaluator moved to the compat lane:
the grammar tests are dependency-free and must keep running on a bare
install, so they cannot share a module with anything importing xarray.
"""

from __future__ import annotations

import pandas as pd

from tests.oracle import builder, xr


class TestWhereEvaluation:
    def _ds(self):
        return xr.Dataset(
            {
                'p_max': xr.DataArray(
                    [100, 0, 50],
                    dims=['g'],
                    coords={'g': ['wind', 'solar', 'gas']},
                ),
            }
        )

    def _mc(self):
        return {'g': pd.Index(['wind', 'solar', 'gas'], name='g')}

    def test_none_returns_scalar_true(self):
        mask = builder.evaluate_where(None, self._ds(), self._mc())
        assert isinstance(mask, xr.DataArray)
        assert mask.ndim == 0
        assert bool(mask) is True

    def test_existence_check(self):
        mask = builder.evaluate_where('p_max', self._ds(), self._mc())
        assert isinstance(mask, xr.DataArray)
        assert mask.all()

    def test_comparison(self):
        mask = builder.evaluate_where('p_max > 0', self._ds(), self._mc())
        assert bool(mask.sel(g='wind')) is True
        assert bool(mask.sel(g='solar')) is False
        assert bool(mask.sel(g='gas')) is True

    def test_missing_param_returns_scalar_false(self):
        mask = builder.evaluate_where('nonexistent', self._ds(), self._mc())
        assert isinstance(mask, xr.DataArray)
        assert mask.ndim == 0
        assert bool(mask) is False

    def test_dimension_comparison(self):
        ds = xr.Dataset()
        mc = {'t': pd.Index([0, 1, 2], name='t')}
        mask = builder.evaluate_where('t > 0', ds, mc)
        assert isinstance(mask, xr.DataArray)
        assert bool(mask.sel(t=0)) is False
        assert bool(mask.sel(t=1)) is True
