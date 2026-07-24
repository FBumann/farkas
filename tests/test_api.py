"""Native API: YAML → streaming engine → solver, with linopy never imported.

The linopy-free guarantee is asserted in a subprocess so conftest's optional
compat import cannot pollute the check.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import linopy_yaml as ly


@pytest.fixture
def dispatch_inputs():
    rng = np.random.default_rng(9)
    n_s = 24
    p_max = pd.Series({'wind': 100.0, 'solar': 60.0, 'gas': 200.0})
    cost = pd.Series({'wind': 1.0, 'solar': 2.0, 'gas': 50.0})
    load = pd.Series(
        (rng.uniform(0.2, 0.8, n_s) * p_max.sum()).round(3),
        index=pd.RangeIndex(n_s, name='snapshot'),
    )
    sources = {'p_max': p_max, 'load': load, 'cost': cost}
    coords = {'snapshot': pd.RangeIndex(n_s, name='snapshot')}
    return sources, coords


def test_solve(dispatch_yaml, dispatch_inputs):
    sources, coords = dispatch_inputs
    sol = ly.solve(dispatch_yaml, sources, coords=coords, memory_limit='256MB')
    try:
        assert sol.status == 'Optimal'
        assert np.isfinite(sol.objective)
        primal = sol.primal('p')
        balance = primal.groupby('snapshot')['value'].sum().sort_index()
        assert np.allclose(balance, sources['load'].sort_index())
    finally:
        sol.close()


def test_build_context_manager_and_write_lp(dispatch_yaml, dispatch_inputs, tmp_path):
    sources, coords = dispatch_inputs
    with ly.build(dispatch_yaml, sources, coords=coords) as ex:
        sol = ex.solve()
        assert sol.status == 'Optimal'
        objective_direct = sol.objective

    lp = ly.write_lp(dispatch_yaml, sources, tmp_path / 'm.lp', coords=coords)
    import highspy

    h = highspy.Highs()
    h.setOptionValue('output_flag', False)
    h.readModel(str(lp))
    h.run()
    assert h.getInfo().objective_function_value == pytest.approx(objective_direct, rel=1e-9)


def test_parquet_path_sources(dispatch_yaml, dispatch_inputs, tmp_path):
    sources, coords = dispatch_inputs
    paths = {}
    for name, series in sources.items():
        dim = 'generator' if name in ('p_max', 'cost') else 'snapshot'
        df = series.rename('value').rename_axis([dim]).reset_index()
        p = tmp_path / f'{name}.parquet'
        df.to_parquet(p, index=False)
        paths[name] = str(p)

    sol = ly.solve(dispatch_yaml, paths, coords=coords)
    try:
        assert sol.status == 'Optimal'
    finally:
        sol.close()

    ref = ly.solve(dispatch_yaml, sources, coords=coords)
    try:
        assert sol.objective == pytest.approx(ref.objective, rel=1e-9)
    finally:
        ref.close()


def test_out_of_language_is_a_build_error(dispatch_yaml, dispatch_inputs):
    import yaml as pyyaml

    from linopy_yaml.relational import RelationalBuildError

    raw = pyyaml.safe_load(Path(dispatch_yaml).read_text())
    raw['objectives']['total_cost']['equations'] = [{'expression': 'sum(p ** 2, over=generator)'}]
    sources, coords = dispatch_inputs
    with pytest.raises(RelationalBuildError, match='operator'):
        ly.build(raw, sources, coords=coords)


def test_runtime_is_linopy_free(dispatch_yaml):
    """Import the package, build and solve a model — linopy never loads."""
    script = textwrap.dedent(f"""
        import sys
        assert "linopy" not in sys.modules

        import pandas as pd
        import linopy_yaml as ly
        assert "linopy" not in sys.modules, "package import pulled in linopy"
        assert "xarray" not in sys.modules, "package import pulled in xarray"

        sol = ly.solve(
            {str(dispatch_yaml)!r},
            {{
                "p_max": pd.Series({{"wind": 100.0, "solar": 60.0, "gas": 200.0}}),
                "cost": pd.Series({{"wind": 1.0, "solar": 2.0, "gas": 50.0}}),
                "load": pd.Series(
                    [80.0, 120.0, 150.0], index=pd.RangeIndex(3, name="snapshot")
                ),
            }},
            coords={{"snapshot": pd.RangeIndex(3, name="snapshot")}},
        )
        assert sol.status == "Optimal"
        sol.close()
        assert "linopy" not in sys.modules, "solve pulled in linopy"
        assert "xarray" not in sys.modules, "solve pulled in xarray"
        print("LINOPY_FREE_OK")
    """)
    out = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    assert 'LINOPY_FREE_OK' in out.stdout


def test_check_needs_no_data(dispatch_yaml):
    schema = ly.check(dispatch_yaml)
    assert 'p' in schema.variables


def test_check_reports_language_errors(dispatch_yaml):
    import yaml as pyyaml

    raw = pyyaml.safe_load(Path(dispatch_yaml).read_text())
    raw['objectives']['total_cost']['equations'] = [{'expression': 'sum(p ** 2, over=generator)'}]
    with pytest.raises(ly.LanguageError, match=r"operator '\*\*'"):
        ly.check(raw)


def test_language_error_is_public_name():
    from linopy_yaml.relational import RelationalBuildError

    assert ly.LanguageError is RelationalBuildError


def test_multi_file_composition_reserved(dispatch_yaml):
    with pytest.raises(NotImplementedError, match='issues/30'):
        ly.check([dispatch_yaml, dispatch_yaml])


def test_write_suffix_dispatch(dispatch_yaml, dispatch_inputs, tmp_path):
    sources, coords = dispatch_inputs
    out = ly.write(dispatch_yaml, sources, tmp_path / 'm.lp', coords=coords)
    assert out.stat().st_size > 0
    with pytest.raises(NotImplementedError, match='mps'):
        ly.write(dispatch_yaml, sources, tmp_path / 'm.mps', coords=coords)
    with pytest.raises(ValueError, match='unsupported output format'):
        ly.write(dispatch_yaml, sources, tmp_path / 'm.nc', coords=coords)


def test_solution_context_manager_and_to_parquet(dispatch_yaml, dispatch_inputs, tmp_path):
    import pyarrow.parquet as pq

    sources, coords = dispatch_inputs
    with ly.solve(dispatch_yaml, sources, coords=coords) as sol:
        assert sol.status == 'Optimal'
        written = sol.to_parquet(tmp_path / 'solution')
        assert set(written) == {'p'}
        table = pq.read_table(written['p'])
        assert set(table.column_names) == {'snapshot', 'generator', 'value'}
        assert table.num_rows == sol.primal('p').shape[0]
    # closed by the with-block: the workdir is gone
    with pytest.raises(Exception):  # noqa: B017 — any error is fine, it must not silently work
        sol.primal('p')


def test_no_helper_registry_anywhere():
    """The Python helper registry is gone from every surface (#38 replaces it)."""
    import linopy_yaml.helpers as helpers

    assert not hasattr(ly, 'register')
    assert not hasattr(helpers, 'register')
