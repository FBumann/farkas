"""Native API: YAML → streaming engine → solver, with linopy never imported.

The linopy-free guarantee is asserted in a subprocess so conftest's optional
compat import cannot pollute the check.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import numpy as np
import pandas as pd
import pytest

import linopy_yaml as ly


@pytest.fixture
def dispatch_inputs():
    rng = np.random.default_rng(9)
    n_s = 24
    p_max = pd.Series({"wind": 100.0, "solar": 60.0, "gas": 200.0})
    cost = pd.Series({"wind": 1.0, "solar": 2.0, "gas": 50.0})
    load = pd.Series(
        (rng.uniform(0.2, 0.8, n_s) * p_max.sum()).round(3),
        index=pd.RangeIndex(n_s, name="snapshot"),
    )
    sources = {"p_max": p_max, "load": load, "cost": cost}
    coords = {"snapshot": pd.RangeIndex(n_s, name="snapshot")}
    return sources, coords


def test_solve(dispatch_yaml, dispatch_inputs):
    sources, coords = dispatch_inputs
    sol = ly.solve(dispatch_yaml, sources, coords=coords, memory_limit="256MB")
    try:
        assert sol.status == "Optimal"
        assert np.isfinite(sol.objective)
        primal = sol.primal("p")
        balance = primal.groupby("snapshot")["value"].sum().sort_index()
        assert np.allclose(balance, sources["load"].sort_index())
    finally:
        sol.close()


def test_build_context_manager_and_write_lp(dispatch_yaml, dispatch_inputs, tmp_path):
    sources, coords = dispatch_inputs
    with ly.build(dispatch_yaml, sources, coords=coords) as ex:
        sol = ex.solve()
        assert sol.status == "Optimal"
        objective_direct = sol.objective

    lp = ly.write_lp(dispatch_yaml, sources, tmp_path / "m.lp", coords=coords)
    import highspy

    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    h.readModel(str(lp))
    h.run()
    assert h.getInfo().objective_function_value == pytest.approx(
        objective_direct, rel=1e-9
    )


def test_parquet_path_sources(dispatch_yaml, dispatch_inputs, tmp_path):
    sources, coords = dispatch_inputs
    paths = {}
    for name, series in sources.items():
        dim = "generator" if name in ("p_max", "cost") else "snapshot"
        df = series.rename("value").rename_axis([dim]).reset_index()
        p = tmp_path / f"{name}.parquet"
        df.to_parquet(p, index=False)
        paths[name] = str(p)

    sol = ly.solve(dispatch_yaml, paths, coords=coords)
    try:
        assert sol.status == "Optimal"
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

    raw = pyyaml.safe_load(open(dispatch_yaml))
    raw["objectives"]["total_cost"]["equations"] = [
        {"expression": "sum(p ** 2, over=generator)"}
    ]
    sources, coords = dispatch_inputs
    with pytest.raises(RelationalBuildError, match="operator"):
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
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=300
    )
    assert out.returncode == 0, out.stderr
    assert "LINOPY_FREE_OK" in out.stdout
