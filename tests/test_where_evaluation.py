"""Eager reading of a where AST — ``builder.evaluate_where``.

Split out of test_parser.py when the evaluator moved to the compat lane:
the grammar tests are dependency-free and must keep running on a bare
install, so they cannot share a module with anything importing xarray.
"""

from __future__ import annotations

import pandas as pd
import pytest

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

    def _ns(self, parameters=('p_max',), dimensions=('g',)):
        from linopy_yaml.resolution import Namespace

        return Namespace((), parameters, dimensions)

    def _where(self, text, ns=None):
        """Resolve then evaluate — the evaluator no longer takes strings."""
        from linopy_yaml.resolution import where_of

        return where_of(text, ns or self._ns(), 'test')

    def test_existence_check(self):
        mask = builder.evaluate_where(self._where('p_max'), self._ds(), self._mc())
        assert isinstance(mask, xr.DataArray)
        assert mask.all()

    def test_comparison(self):
        mask = builder.evaluate_where(self._where('p_max > 0'), self._ds(), self._mc())
        assert bool(mask.sel(g='wind')) is True
        assert bool(mask.sel(g='solar')) is False
        assert bool(mask.sel(g='gas')) is True

    def test_missing_param_is_a_load_error(self):
        """Was: a scalar-False mask, i.e. a silently empty model. Resolution
        makes an undeclared name a load error in both lanes."""
        from linopy_yaml.errors import LanguageError

        with pytest.raises(LanguageError, match="'nonexistent' not found"):
            self._where('nonexistent')

    def test_dimension_comparison(self):
        ds = xr.Dataset()
        mc = {'t': pd.Index([0, 1, 2], name='t')}
        node = self._where('t > 0', self._ns(parameters=(), dimensions=('t',)))
        mask = builder.evaluate_where(node, ds, mc)
        assert isinstance(mask, xr.DataArray)
        assert bool(mask.sel(t=0)) is False
        assert bool(mask.sel(t=1)) is True
