"""roll: the storage YAML (time-coupled soc) through both backends.

examples/storage.yaml is dispatch plus a cyclic battery:
soc == roll(soc, snapshot=1) + charge * 0.9 - discharge. The eager backend
implements roll with linopy's circular .roll(); the relational backend lowers
it to ir.Shift — a pointwise ord-join remap.
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
from linopy_yaml.relational import (  # noqa: E402
    DuckdbExecutor,
    RelationalBuildError,
    Shift,
    Var,
)
from linopy_yaml.schema import MathSchema  # noqa: E402

RTOL = 1e-9

STORAGE_YAML = "examples/storage.yaml"


@pytest.fixture
def storage_inputs():
    """Peaky load that exceeds generation capacity at the peaks, so the
    battery is *required* (not just economic) and soc is genuinely coupled."""
    n_s = 48
    p_max = pd.Series({"wind": 80.0, "gas": 70.0})
    cost = pd.Series({"wind": 1.0, "gas": 40.0})
    t = np.arange(n_s)
    load = pd.Series(
        (110 + 60 * np.sin(2 * np.pi * t / 24)).round(3),  # peaks at 170 > 150
        index=pd.RangeIndex(n_s, name="snapshot"),
    )
    data = {"p_max": p_max, "cost": cost, "load": load}
    coords = {
        "snapshot": pd.RangeIndex(n_s, name="snapshot"),
        "generator": pd.Index(p_max.index, name="generator"),
    }
    return data, coords


def test_storage_yaml_differential(storage_inputs, tmp_path):
    data, coords = storage_inputs

    m = Model.from_yaml(STORAGE_YAML, data=data, coords=coords)
    m.solve(solver_name="highs", output_flag=False)
    oracle = float(m.objective.value)
    assert np.isfinite(oracle)
    # the battery must actually cycle for the model to be feasible
    assert float(m.solution["discharge"].max()) > 1e-3

    schema = MathSchema(**pyyaml.safe_load(open(STORAGE_YAML)))
    with DuckdbExecutor(memory_limit="256MB") as ex:
        ex.build(lower_program(schema), tidy_sources(schema, data, coords))

        sol = ex.solve()
        assert sol.status == "Optimal"
        assert sol.objective == pytest.approx(oracle, rel=RTOL)

        lp = tmp_path / "storage.lp"
        ex.write_lp(lp)
        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        h.readModel(str(lp))
        h.run()
        assert h.getInfo().objective_function_value == pytest.approx(oracle, rel=RTOL)

        # the relational soc trace satisfies the cyclic recurrence
        soc = sol.primal("soc").set_index("snapshot")["value"].sort_index()
        charge = sol.primal("charge").set_index("snapshot")["value"].sort_index()
        discharge = sol.primal("discharge").set_index("snapshot")["value"].sort_index()
        soc_prev = np.roll(soc.to_numpy(), 1)
        assert np.allclose(
            soc.to_numpy(),
            soc_prev + 0.9 * charge.to_numpy() - discharge.to_numpy(),
            atol=1e-6,
        )


def test_roll_lowering_structure():
    schema = MathSchema(**pyyaml.safe_load(open(STORAGE_YAML)))
    from linopy_yaml.expression_parser import parse_expression
    from linopy_yaml.lowering import _lower_expr

    ast = parse_expression("roll(soc, snapshot=1)")
    assert _lower_expr(ast, schema, "t") == Shift(Var("soc"), "snapshot", 1)

    # negative shifts work (look-ahead)
    ast = parse_expression("roll(soc, snapshot=-2)")
    assert _lower_expr(ast, schema, "t") == Shift(Var("soc"), "snapshot", -2)


def test_roll_lowering_errors():
    schema = MathSchema(**pyyaml.safe_load(open(STORAGE_YAML)))
    from linopy_yaml.expression_parser import parse_expression
    from linopy_yaml.lowering import _lower_expr

    with pytest.raises(RelationalBuildError, match="dimension 'nope' is not declared"):
        _lower_expr(parse_expression("roll(soc, nope=1)"), schema, "t")
    with pytest.raises(RelationalBuildError, match="but the expression has dims"):
        _lower_expr(parse_expression("roll(load, generator=1)"), schema, "t")
