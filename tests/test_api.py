"""Native API: YAML → streaming engine → solver, with linopy never imported.

The linopy-free guarantee is asserted in a subprocess so conftest's optional
farkas_linopy import cannot pollute the check.

This module is deliberately **pandas-free**: it is the bare install's proof
that the native path — frames in, build, solve, frames out — needs no
dataframe library beyond the engine's own. The tests that exercise the bridges
*out* (``to_pandas``, ``to_dataarray``) say so with an ``importorskip``.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import numpy as np
import polars as pl
import pytest

import farkas as fk
from tests.conftest import schema_of, solve_lp_file


def test_solve(dispatch_yaml, dispatch_frame_inputs):
    sources, coords = dispatch_frame_inputs
    result = fk.solve(dispatch_yaml, sources, coords=coords)
    try:
        assert result.is_ok
        assert np.isfinite(result.objective)
        balance = result.primal('p').group_by('snapshot').agg(pl.col('value').sum()).sort('snapshot')
        assert np.allclose(balance['value'], sources['load']['value'])
    finally:
        result.close()


def test_build_context_manager_and_write_lp(dispatch_yaml, dispatch_frame_inputs, tmp_path):
    sources, coords = dispatch_frame_inputs
    with fk.build(dispatch_yaml, sources, coords=coords) as ex:
        result = ex.solve()
        assert result.is_ok
        objective_direct = result.objective

    lp = fk.write(dispatch_yaml, sources, tmp_path / 'm.lp', coords=coords)
    assert solve_lp_file(lp) == pytest.approx(objective_direct, rel=1e-9)


def test_parquet_path_sources(dispatch_yaml, dispatch_frame_inputs, tmp_path):
    sources, coords = dispatch_frame_inputs
    paths = {}
    for name, frame in sources.items():
        p = tmp_path / f'{name}.parquet'
        frame.write_parquet(p)
        paths[name] = str(p)

    result = fk.solve(dispatch_yaml, paths, coords=coords)
    try:
        assert result.is_ok
    finally:
        result.close()

    ref = fk.solve(dispatch_yaml, sources, coords=coords)
    try:
        assert result.objective == pytest.approx(ref.objective, rel=1e-9)
    finally:
        ref.close()


def test_runtime_is_linopy_free(dispatch_yaml):
    """Import the package, build and solve on Arrow sources — linopy never loads.

    pandas and pyarrow are on the list too, and that is newer than it looks:
    on the duckdb engine they could not be, because duckdb imported pandas
    opportunistically when registering any Python object, so "not in
    ``sys.modules``" was not a claim this package could keep. polars imports
    neither until asked, so the stronger claim is now available and is pinned
    here — a bridge out (``to_pandas``, ``to_dataarray``) must stay a bridge
    and never become something the build path walks over on its own.

    Distinct from, and weaker than, the claim that they need not be
    *installed*: the bare-install CI job is what proves that, running this
    suite with no dataframe library beyond polars present at all.
    """
    absent = ('linopy', 'xarray', 'pandas', 'pyarrow')
    script = textwrap.dedent(f"""
        import sys
        assert "linopy" not in sys.modules

        import polars as pl
        import farkas as fk
        for lib in {absent!r}:
            assert lib not in sys.modules, f"package import pulled in {{lib}}"

        result = fk.solve(
            {str(dispatch_yaml)!r},
            {{
                "p_max": pl.DataFrame({{"generator": ["wind", "solar", "gas"],
                                       "value": [100.0, 60.0, 200.0]}}),
                "cost": pl.DataFrame({{"generator": ["wind", "solar", "gas"],
                                      "value": [1.0, 2.0, 50.0]}}),
                "load": pl.DataFrame({{"snapshot": [0, 1, 2],
                                      "value": [80.0, 120.0, 150.0]}}),
            }},
            coords={{"snapshot": range(3)}},
        )
        assert result.is_ok
        # the whole round trip stayed in Arrow: no dataframe on either side
        assert isinstance(result.primal("p"), pl.DataFrame)
        assert result.primal("p").height == 9
        result.close()
        for lib in {absent!r}:
            assert lib not in sys.modules, f"solve pulled in {{lib}}"
        print("LINOPY_FREE_OK")
    """)
    out = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    assert 'LINOPY_FREE_OK' in out.stdout


def test_check_and_load_schema_need_no_data(dispatch_yaml):
    """The model stands for itself: the schema is read from the file when
    wanted, never carried on a built model."""
    for schema in (fk.check(dispatch_yaml), fk.load_schema(dispatch_yaml)):
        assert schema.variables['p'].foreach == ['snapshot', 'generator']
        assert schema.parameters['load'].dims == ['snapshot']


@pytest.mark.parametrize(
    ('expression', 'match'),
    [
        ('sum(p ** 2, over=generator)', r"operator '\*\*'"),
        # the CI verb enforces degree 1 with no data bound (ROADMAP, degree axis)
        ('sum(p * p, over=generator)', 'degree 2'),
    ],
)
def test_check_reports_language_errors_before_any_data_is_bound(
    dispatch_yaml, dispatch_frame_inputs, expression, match
):
    raw = schema_of(dispatch_yaml, **{'objectives.total_cost.equations': [{'expression': expression}]}).model_dump()

    with pytest.raises(fk.LanguageError, match=match):
        fk.check(raw)
    # ...and build says the same thing rather than deferring it to the solver
    sources, coords = dispatch_frame_inputs
    with pytest.raises(fk.LanguageError, match=match):
        fk.build(raw, sources, coords=coords)


def test_error_hierarchy_is_one_catchable_tree():
    """One ``except`` covers the package, and the model/run split is real."""
    from farkas.relational import RelationalBuildError

    for cls in (fk.LanguageError, fk.DataError):
        assert issubclass(cls, fk.LinopyYamlError)
    for cls in (fk.SchemaError, fk.DimensionError, fk.PiecewiseExpansionError):
        assert issubclass(cls, fk.LanguageError)
    assert not issubclass(fk.DataError, fk.LanguageError)
    assert issubclass(fk.LinopyYamlError, ValueError)

    # the retired name still catches everything it used to
    assert RelationalBuildError is fk.LinopyYamlError


def test_multi_file_composition_reserved(dispatch_yaml):
    with pytest.raises(NotImplementedError, match='issues/30'):
        fk.check([dispatch_yaml, dispatch_yaml])


def test_write_suffix_dispatch(dispatch_yaml, dispatch_frame_inputs, tmp_path):
    sources, coords = dispatch_frame_inputs
    out = fk.write(dispatch_yaml, sources, tmp_path / 'm.lp', coords=coords)
    assert out.stat().st_size > 0
    with pytest.raises(NotImplementedError, match='mps'):
        fk.write(dispatch_yaml, sources, tmp_path / 'm.mps', coords=coords)
    with pytest.raises(ValueError, match='unsupported output format'):
        fk.write(dispatch_yaml, sources, tmp_path / 'm.nc', coords=coords)


def test_solution_to_parquet(dispatch_yaml, dispatch_frame_inputs, tmp_path):
    """One file per variable, tidy, streamed straight to disk."""
    sources, coords = dispatch_frame_inputs
    result = fk.solve(dispatch_yaml, sources, coords=coords)
    assert result.is_ok
    written = result.to_parquet(tmp_path / 'solution')
    assert set(written) == {'p'}
    frame = pl.read_parquet(written['p'])
    assert set(frame.columns) == {'snapshot', 'generator', 'value'}
    assert frame.height == result.primal('p').height


def test_a_result_stays_readable_until_it_is_closed(dispatch_yaml, dispatch_frame_inputs):
    """No lifetime to manage: reading is valid until you say otherwise.

    The built model is frames this process owns, so nothing expires on its own
    and a caller who never closes loses nothing but memory. `close()` is there
    to hand a large model back early, and it means what it says — after it,
    there is nothing left to read.
    """
    sources, coords = dispatch_frame_inputs
    result = fk.solve(dispatch_yaml, sources, coords=coords)
    height = result.primal('p').height
    assert height > 0
    assert result.primal('p').height == height  # still there, no close in sight

    result.close()
    with pytest.raises(AssertionError):
        result.primal('p')


def test_primal_is_a_frame_and_to_pandas_is_the_bridge(dispatch_yaml, dispatch_frame_inputs):
    """A frame is the shape results come in; pandas is an exit, not a shape.

    The two must describe the same table — the bridge is a conversion, not a
    second query with its own opinion about column order or dtypes.
    """
    sources, coords = dispatch_frame_inputs
    result = fk.solve(dispatch_yaml, sources, coords=coords)
    frame = result.primal('p')
    assert isinstance(frame, pl.DataFrame)
    assert frame.columns == ['snapshot', 'generator', 'value']

    pandas = pytest.importorskip('pandas')
    converted = result.to_pandas('p')
    assert isinstance(converted, pandas.DataFrame)
    assert list(converted.columns) == frame.columns
    assert len(converted) == frame.height
    assert frame['value'].sum() == pytest.approx(converted['value'].sum())


def test_no_helper_registry_anywhere():
    """The helper set is closed — there is no way to register more, on any
    surface (#38's ``escape:`` island replaces the idea).

    This is what makes the two lanes accept the same language, and hence what
    makes the differential tests an oracle rather than a comparison of
    dialects (ARCHITECTURE.md, "The expressive ceiling").
    """
    import farkas.helpers as helpers

    assert not hasattr(fk, 'register')
    assert not hasattr(helpers, 'register')
    assert not hasattr(helpers, '_REGISTRY')


def test_solution_to_dataarray(dispatch_yaml, dispatch_frame_inputs):
    """Long tables are right for joining, wrong for the array math that
    post-processing is mostly made of. `to_dataarray` is the bridge."""
    pytest.importorskip('xarray')
    sources, coords = dispatch_frame_inputs

    with fk.solve(dispatch_yaml, sources, coords=coords) as result:
        arr = result.to_dataarray('p')
        tidy = result.to_pandas('p')

    assert arr.name == 'p'  # not 'value', the tidy column it came from
    assert sorted(arr.dims) == ['generator', 'snapshot']
    assert arr.sizes['generator'] == 3
    # the labelled form is the tidy form, indexed
    wind_0 = tidy.query("generator == 'wind' and snapshot == 0")['value'].iloc[0]
    assert float(arr.sel(generator='wind', snapshot=0)) == pytest.approx(wind_0)


def test_solution_to_dataset(dispatch_yaml, dispatch_frame_inputs):
    """Several variables at once, each keeping its own dims."""
    pytest.importorskip('xarray')
    sources, coords = dispatch_frame_inputs

    with fk.solve(dispatch_yaml, sources, coords=coords) as result:
        ds = result.to_dataset('p')
        tidy = result.to_pandas('p')

    assert list(ds.data_vars) == ['p']
    assert sorted(ds['p'].dims) == ['generator', 'snapshot']
    first = tidy.iloc[0]
    assert float(ds['p'].sel(snapshot=first['snapshot'], generator=first['generator'])) == pytest.approx(first['value'])


TWO_VARIABLE_MODEL = {
    'dimensions': {'snapshot': {'dtype': 'int'}, 'generator': {'values': ['wind', 'gas']}},
    'parameters': {'p_max': {'dims': ['generator']}, 'load': {'dims': ['snapshot']}},
    'variables': {
        'p': {'foreach': ['snapshot', 'generator'], 'bounds': {'lower': 0, 'upper': 'p_max'}},
        'shed': {'foreach': ['snapshot'], 'bounds': {'lower': 0}},
    },
    'constraints': {
        'balance': {
            'foreach': ['snapshot'],
            'equations': [{'expression': 'sum(p, over=generator) + shed == load'}],
        }
    },
    'objectives': {'total': {'sense': 'minimize', 'equations': [{'expression': 'shed'}]}},
}


def test_to_dataset_defaults_to_every_variable():
    """A small model wants all of them at once, as linopy's model.solution
    gives you — naming them would be busywork."""
    pytest.importorskip('xarray')
    n = 4
    sources = {
        'p_max': pl.DataFrame({'generator': ['wind', 'gas'], 'value': [100.0, 200.0]}),
        'load': pl.DataFrame({'snapshot': list(range(n)), 'value': np.full(n, 90.0)}),
    }

    with fk.solve(TWO_VARIABLE_MODEL, sources, coords={'snapshot': range(n)}) as result:
        ds = result.to_dataset()
        subset = result.to_dataset('shed')

    assert set(ds.data_vars) == {'p', 'shed'}
    assert sorted(ds['p'].dims) == ['generator', 'snapshot']
    assert list(ds['shed'].dims) == ['snapshot']  # keeps its own dims
    assert set(subset.data_vars) == {'shed'}
