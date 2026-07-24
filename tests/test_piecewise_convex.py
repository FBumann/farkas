"""Convex piecewise cost via epigraph — pure LP, both backends, no new constructs.

Demonstrates SPEC §12's claim: convex piecewise needs no formulation
machinery at all. The epigraph constraints are ordinary affine YAML, so the
model is relational-eligible today. Correctness is checked against a numpy
evaluation of the piecewise cost at the optimal dispatch.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

duckdb = pytest.importorskip("duckdb")
highspy = pytest.importorskip("highspy")

import yaml as pyyaml  # noqa: E402
from linopy import Model  # noqa: E402

import linopy_yaml  # noqa: F401, E402 — registers .from_yaml on linopy.Model
from linopy_yaml.lowering import lower_program, tidy_sources  # noqa: E402
from linopy_yaml.relational import DuckdbExecutor  # noqa: E402
from linopy_yaml.router import select_backend  # noqa: E402
from linopy_yaml.schema import MathSchema  # noqa: E402

RTOL = 1e-9

PWL_YAML = "examples/piecewise_convex.yaml"


@pytest.fixture
def pwl_inputs():
    rng = np.random.default_rng(9)
    n_s = 24
    gens = ["cheap", "mid"]
    segments = ["s0", "s1", "s2"]
    p_max = pd.Series({"cheap": 100.0, "mid": 120.0})

    # convex piecewise cost: increasing marginal cost per segment.
    # tangent k of a convex curve: cost >= slope_k * p + intercept_k
    slopes = pd.DataFrame(
        {"cheap": [5.0, 15.0, 40.0], "mid": [20.0, 35.0, 60.0]}, index=segments
    )
    # breakpoints at 40% and 75% of p_max; intercepts make tangents touch
    intercepts = {}
    for g in gens:
        b1, b2 = 0.4 * p_max[g], 0.75 * p_max[g]
        s0, s1, s2 = slopes[g]
        intercepts[g] = [0.0, (s0 - s1) * b1, (s0 - s1) * b1 + (s1 - s2) * b2]
    icepts = pd.DataFrame(intercepts, index=segments)

    load = pd.Series(
        (rng.uniform(0.3, 0.9, n_s) * p_max.sum()).round(1),
        index=pd.RangeIndex(n_s, name="snapshot"),
    )
    import xarray as xr

    data = {
        "p_max": p_max,
        "load": load,
        "seg_slope": xr.DataArray.from_series(
            slopes.T.stack().rename_axis(["generator", "segment"])
        ),
        "seg_intercept": xr.DataArray.from_series(
            icepts.T.stack().rename_axis(["generator", "segment"])
        ),
    }
    coords = {
        "snapshot": load.index,
        "generator": pd.Index(gens, name="generator"),
        "segment": pd.Index(segments, name="segment"),
    }
    return data, coords


def pwl_cost(p: float, slopes, icepts) -> float:
    return max(s * p + i for s, i in zip(slopes, icepts))


def test_pwl_convex_differential(pwl_inputs, tmp_path):
    data, coords = pwl_inputs

    schema = MathSchema(**pyyaml.safe_load(open(PWL_YAML)))
    assert select_backend(schema).backend == "relational"  # eligible, pure LP

    m = Model.from_yaml(PWL_YAML, data=data, coords=coords)
    m.solve(solver_name="highs", output_flag=False)
    oracle = float(m.objective.value)
    assert np.isfinite(oracle)

    with DuckdbExecutor(memory_limit="256MB") as ex:
        ex.build(lower_program(schema), tidy_sources(schema, data, coords))
        sol = ex.solve()
        assert sol.status == "Optimal"
        assert sol.objective == pytest.approx(oracle, rel=RTOL)

        lp = tmp_path / "pwl.lp"
        ex.write_lp(lp)
        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        h.readModel(str(lp))
        h.run()
        assert h.getInfo().objective_function_value == pytest.approx(oracle, rel=RTOL)

        # gen_cost equals the true piecewise cost at the optimal dispatch
        # (epigraph is tight under minimisation)
        p = sol.primal("p").set_index(["snapshot", "generator"])["value"]
        gc = sol.primal("gen_cost").set_index(["snapshot", "generator"])["value"]
        slopes = data["seg_slope"].to_series().unstack("segment")
        icepts = data["seg_intercept"].to_series().unstack("segment")
        for (s, g), pv in p.items():
            expected = pwl_cost(pv, slopes.loc[g], icepts.loc[g])
            assert gc[(s, g)] == pytest.approx(expected, abs=1e-6)
