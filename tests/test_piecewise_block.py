"""piecewise: blocks — λ expansion, both backends, linopy-shaped links.

The expansion runs before either backend, so eager and relational receive
identical affine declarations. Nonconvex correctness is verified by checking
the linked primals lie ON the curve (adjacency binaries at work) against a
numpy interpolation; the convex flag is verified to produce the hull instead.
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
from linopy_yaml.piecewise import (  # noqa: E402
    PiecewiseExpansionError,
    expand_piecewise,
)
from linopy_yaml.relational import DuckdbExecutor  # noqa: E402
from linopy_yaml.schema import MathSchema  # noqa: E402

RTOL = 1e-9

NONCONVEX_YAML = """
dimensions:
  snapshot: {dtype: int}
  bp: {dtype: int}

parameters:
  load: {dims: [snapshot]}
  bp_x: {dims: [bp]}
  bp_y: {dims: [bp]}

variables:
  p:
    foreach: [snapshot]
    bounds: {lower: 0, upper: 100}
  op_cost:
    foreach: [snapshot]
    bounds: {lower: 0}

piecewise:
  cost_curve:
    over: bp
    links:
      - [p, bp_x]
      - [op_cost, bp_y]

constraints:
  balance:
    foreach: [snapshot]
    equations:
      - expression: p == load

objectives:
  total:
    sense: minimize
    equations:
      - expression: sum(op_cost, over=snapshot)
"""


@pytest.fixture
def nonconvex_inputs():
    rng = np.random.default_rng(13)
    n_s = 12
    # concave curve (economies of scale): slopes 0.75 then ~0.417 — the
    # convex hull's lower envelope (the chord) would undercut it, so the
    # adjacency binaries are load-bearing
    bp_x = pd.Series([0.0, 40.0, 100.0], index=pd.RangeIndex(3, name="bp"))
    bp_y = pd.Series([0.0, 30.0, 55.0], index=pd.RangeIndex(3, name="bp"))
    load = pd.Series(
        rng.uniform(5, 95, n_s).round(2), index=pd.RangeIndex(n_s, name="snapshot")
    )
    data = {"load": load, "bp_x": bp_x, "bp_y": bp_y}
    coords = {"snapshot": load.index, "bp": bp_x.index}
    return data, coords


def curve(p, bp_x, bp_y):
    return float(np.interp(p, bp_x.to_numpy(), bp_y.to_numpy()))


def test_nonconvex_on_curve_differential(nonconvex_inputs, tmp_path):
    data, coords = nonconvex_inputs
    yaml_path = tmp_path / "pw.yaml"
    yaml_path.write_text(NONCONVEX_YAML)

    m = Model.from_yaml(yaml_path, data=data, coords=coords)
    m.solve(solver_name="highs", output_flag=False)
    oracle = float(m.objective.value)
    expected = sum(curve(v, data["bp_x"], data["bp_y"]) for v in data["load"])
    assert oracle == pytest.approx(expected, rel=1e-6)  # ON the curve, not the hull

    schema = MathSchema(**pyyaml.safe_load(NONCONVEX_YAML))
    with DuckdbExecutor(memory_limit="256MB") as ex:
        ex.build(lower_program(schema), tidy_sources(schema, data, coords))
        sol = ex.solve()
        assert sol.status == "Optimal"
        assert sol.objective == pytest.approx(oracle, rel=RTOL)

        cost = sol.primal("op_cost").set_index("snapshot")["value"]
        for s, load_v in data["load"].items():
            assert cost[s] == pytest.approx(
                curve(load_v, data["bp_x"], data["bp_y"]), abs=1e-6
            )


def test_convex_flag_gives_hull(nonconvex_inputs, tmp_path):
    # same concave curve with convex: true — the LP relaxation's lower
    # envelope is the chord, so the objective must drop BELOW the curve
    data, coords = nonconvex_inputs
    yaml_text = NONCONVEX_YAML.replace("over: bp", "over: bp\n    convex: true")
    yaml_path = tmp_path / "pw_hull.yaml"
    yaml_path.write_text(yaml_text)

    schema = MathSchema(**pyyaml.safe_load(yaml_text))
    from linopy_yaml.router import select_backend

    assert select_backend(schema).backend == "relational"  # pure LP now
    program = lower_program(schema)
    assert all(v.vtype == "continuous" for v in program.variables)

    m = Model.from_yaml(yaml_path, data=data, coords=coords)
    m.solve(solver_name="highs", output_flag=False)
    on_curve = sum(curve(v, data["bp_x"], data["bp_y"]) for v in data["load"])
    chord = sum(0.55 * v for v in data["load"])  # (100, 55) chord from origin
    assert float(m.objective.value) == pytest.approx(chord, rel=1e-6)
    assert float(m.objective.value) < on_curve


CHP_YAML = """
dimensions:
  snapshot: {dtype: int}
  bp: {dtype: int}

parameters:
  load: {dims: [snapshot]}
  power_bp: {dims: [bp]}
  fuel_bp: {dims: [bp]}
  heat_bp: {dims: [bp]}

variables:
  power:
    foreach: [snapshot]
    bounds: {lower: 0, upper: 100}
  fuel:
    foreach: [snapshot]
    bounds: {lower: 0}
  heat:
    foreach: [snapshot]
    bounds: {lower: 0}

piecewise:
  chp:
    over: bp
    links:
      - [power, power_bp]
      - [fuel, fuel_bp]
      - [heat, heat_bp]

constraints:
  balance:
    foreach: [snapshot]
    equations:
      - expression: power == load

objectives:
  total:
    sense: minimize
    equations:
      - expression: sum(fuel, over=snapshot)
"""


def test_chp_three_links(tmp_path):
    n_s = 8
    rng = np.random.default_rng(21)
    power_bp = pd.Series([0.0, 50.0, 100.0], index=pd.RangeIndex(3, name="bp"))
    fuel_bp = pd.Series([10.0, 60.0, 140.0], index=pd.RangeIndex(3, name="bp"))
    heat_bp = pd.Series([0.0, 20.0, 60.0], index=pd.RangeIndex(3, name="bp"))
    load = pd.Series(
        rng.uniform(10, 90, n_s).round(2), index=pd.RangeIndex(n_s, name="snapshot")
    )
    data = {"load": load, "power_bp": power_bp, "fuel_bp": fuel_bp, "heat_bp": heat_bp}
    coords = {"snapshot": load.index, "bp": power_bp.index}

    yaml_path = tmp_path / "chp.yaml"
    yaml_path.write_text(CHP_YAML)
    m = Model.from_yaml(yaml_path, data=data, coords=coords)
    m.solve(solver_name="highs", output_flag=False)
    oracle = float(m.objective.value)
    assert np.isfinite(oracle)

    schema = MathSchema(**pyyaml.safe_load(CHP_YAML))
    with DuckdbExecutor(memory_limit="256MB") as ex:
        ex.build(lower_program(schema), tidy_sources(schema, data, coords))
        sol = ex.solve()
        assert sol.status == "Optimal"
        assert sol.objective == pytest.approx(oracle, rel=RTOL)

        # all three linked primals sit at the same curve position
        fuel = sol.primal("fuel").set_index("snapshot")["value"]
        heat = sol.primal("heat").set_index("snapshot")["value"]
        for s, load_v in load.items():
            assert fuel[s] == pytest.approx(curve(load_v, power_bp, fuel_bp), abs=1e-6)
            assert heat[s] == pytest.approx(curve(load_v, power_bp, heat_bp), abs=1e-6)


def test_expansion_structure_and_errors():
    schema = MathSchema(**pyyaml.safe_load(NONCONVEX_YAML))
    expanded = expand_piecewise(schema)
    assert not expanded.piecewise
    assert "cost_curve_lam" in expanded.variables
    assert expanded.variables["cost_curve_seg"].binary
    assert set(expanded.constraints) >= {
        "cost_curve_convexity",
        "cost_curve_pick",
        "cost_curve_adjacency",
        "cost_curve_link0",
        "cost_curve_link1",
        "balance",
    }
    # inline expressions are legal links
    raw = pyyaml.safe_load(NONCONVEX_YAML)
    raw["piecewise"]["cost_curve"]["links"][0] = ["p * 2", "bp_x"]
    expanded = expand_piecewise(MathSchema(**raw))
    eq = expanded.constraints["cost_curve_link0"].equations[0].expression
    assert eq.startswith("(p * 2) ==")

    raw = pyyaml.safe_load(NONCONVEX_YAML)
    raw["piecewise"]["cost_curve"]["links"][1][1] = "nope"
    with pytest.raises(PiecewiseExpansionError, match="undeclared parameter 'nope'"):
        expand_piecewise(MathSchema(**raw))

    raw = pyyaml.safe_load(NONCONVEX_YAML)
    raw["piecewise"]["cost_curve"]["links"] = [
        ["p", "bp_x", "<="],
        ["op_cost", "bp_y", ">="],
    ]
    with pytest.raises(ValueError, match="at most one link"):
        MathSchema(**raw)
