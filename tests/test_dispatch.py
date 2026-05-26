"""Integration test: the dispatch example from the spec."""

import pandas as pd
import pytest

import linopy_yaml  # noqa: F401 — registers .from_yaml / .yaml on linopy.Model
from linopy import Model


@pytest.mark.xfail(reason="upstream linopy bug: as_dataarray(scalar, coords=2D) fails")
def test_dispatch_builds(dispatch_yaml):
    """Build the dispatch model from YAML and verify structure."""
    m = Model.from_yaml(
        dispatch_yaml,
        data={
            "p_max": pd.Series({"wind": 100, "solar": 60, "gas": 200}),
            "load": pd.Series(
                [80, 120, 150, 180, 140, 100],
                index=pd.RangeIndex(6, name="snapshot"),
            ),
            "cost": pd.Series({"wind": 0, "solar": 0, "gas": 50}),
        },
        coords={
            "snapshot": pd.RangeIndex(6, name="snapshot"),
        },
    )

    # Variables
    assert "p" in m.variables

    # Constraints
    assert "power_balance" in m.constraints

    # Objective was set
    assert m.objective is not None

    # YAML accessor is available
    assert m.yaml.schema.variables["p"].foreach == ["snapshot", "generator"]
    assert m.yaml.schema.parameters["load"].dims == ["snapshot"]
    assert "p_max" in m.yaml.dataset
    assert "load" in m.yaml.dataset


@pytest.mark.xfail(reason="upstream linopy bug: as_dataarray(scalar, coords=2D) fails")
def test_dispatch_solves(dispatch_yaml):
    """Build and solve the dispatch model, check solution is feasible."""
    m = Model.from_yaml(
        dispatch_yaml,
        data={
            "p_max": pd.Series({"wind": 100, "solar": 60, "gas": 200}),
            "load": pd.Series(
                [80, 120, 150, 180, 140, 100],
                index=pd.RangeIndex(6, name="snapshot"),
            ),
            "cost": pd.Series({"wind": 0, "solar": 0, "gas": 50}),
        },
        coords={
            "snapshot": pd.RangeIndex(6, name="snapshot"),
        },
    )

    status = m.solve(solver_name="highs")
    assert status[0] == "ok"

    # Check solution: all generation non-negative
    p_sol = m.solution["p"]
    assert (p_sol >= -1e-6).all()

    # Check power balance is satisfied
    for t in range(6):
        load_t = [80, 120, 150, 180, 140, 100][t]
        gen_sum = float(p_sol.sel(snapshot=t).sum())
        assert abs(gen_sum - load_t) < 1e-4, f"Balance violated at t={t}"


