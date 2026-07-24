"""Phase-3 gate: YAML lowers to the IR and matches the eager backend.

The dispatch example YAML runs through both backends with the same data:
eager `Model.from_yaml(...).solve()` is the oracle; the lowered Program
executes on DuckdbExecutor via solver_direct and the lp_file sink.
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
    Bool,
    Cmp,
    Defined,
    DuckdbExecutor,
    Param,
    RelationalBuildError,
    Sum,
    Var,
)
from linopy_yaml.schema import MathSchema  # noqa: E402

RTOL = 1e-9

DISPATCH_YAML = "examples/dispatch.yaml"


@pytest.fixture
def dispatch_schema() -> MathSchema:
    with open(DISPATCH_YAML) as f:
        return MathSchema(**pyyaml.safe_load(f))


@pytest.fixture
def dispatch_inputs():
    rng = np.random.default_rng(3)
    n_s = 48
    p_max = pd.Series({"wind": 100.0, "solar": 60.0, "gas": 200.0})
    # distinct costs -> unique optimal vertex, so primals are comparable
    cost = pd.Series({"wind": 1.0, "solar": 2.0, "gas": 50.0})
    load = pd.Series(
        (rng.uniform(0.2, 0.8, n_s) * p_max.sum()).round(3),
        index=pd.RangeIndex(n_s, name="snapshot"),
    )
    data = {"p_max": p_max, "load": load, "cost": cost}
    coords = {"snapshot": pd.RangeIndex(n_s, name="snapshot")}
    return data, coords


def test_dispatch_yaml_differential(dispatch_schema, dispatch_inputs, tmp_path):
    data, coords = dispatch_inputs

    # oracle: the eager backend
    m = Model.from_yaml(DISPATCH_YAML, data=data, coords=coords)
    m.solve(solver_name="highs", output_flag=False)
    oracle = float(m.objective.value)
    assert np.isfinite(oracle)

    # relational: lower the same schema, execute with the same inputs
    program = lower_program(dispatch_schema)
    sources = tidy_sources(dispatch_schema, data, coords)

    with DuckdbExecutor(memory_limit="256MB") as ex:
        ex.build(program, sources)

        sol = ex.solve()
        assert sol.status == "Optimal"
        assert sol.objective == pytest.approx(oracle, rel=RTOL)

        lp = tmp_path / "dispatch.lp"
        ex.write_lp(lp)
        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        h.readModel(str(lp))
        h.run()
        assert h.getInfo().objective_function_value == pytest.approx(oracle, rel=RTOL)

        # primal agrees with the eager solution variable-by-variable
        eager_p = m.solution["p"].to_dataframe(name="value").reset_index()
        rel_p = sol.primal("p")
        merged = eager_p.merge(
            rel_p, on=["snapshot", "generator"], suffixes=("_eager", "_rel")
        )
        # masked (gas is unmasked; all p_max > 0 here) rows align 1:1
        assert len(merged) == len(rel_p)
        assert np.allclose(merged["value_eager"], merged["value_rel"], atol=1e-6)


def test_lower_program_structure(dispatch_schema):
    program = lower_program(dispatch_schema)

    assert [p.name for p in program.parameters] == ["p_max", "load", "cost"]
    (v,) = program.variables
    assert v.name == "p"
    assert v.dims == ("snapshot", "generator")
    assert v.where == Cmp("p_max", ">", 0.0)
    assert v.upper == Param("p_max")

    (c,) = program.constraints
    assert c.name == "power_balance"
    assert c.dims == ("snapshot",)
    assert c.lhs == Sum(Var("p"), ("generator",))
    assert c.sense == "=="
    assert c.rhs == Param("load")

    assert program.objective.sense == "min"
    assert program.objective.expr == Sum(Var("p") * Param("cost"), ("generator",))


def test_where_lowering(dispatch_schema):
    from linopy_yaml.lowering import _lower_where

    schema = dispatch_schema
    assert _lower_where(None, schema, "t") is None
    assert _lower_where("True", schema, "t") is None  # True == no mask
    assert _lower_where("p_max", schema, "t") == Defined("p_max")
    assert _lower_where("no_such_param", schema, "t") == Bool(False)
    pred = _lower_where("p_max > 0 AND NOT load == 0", schema, "t")
    assert pred is not None

    with pytest.raises(RelationalBuildError, match="dimension 'snapshot'"):
        _lower_where("snapshot > 5", schema, "t")


def test_sum_over_absent_dim_is_noop(dispatch_schema):
    # eager parity: sum(load, over=generator) leaves load unchanged
    from linopy_yaml.expression_parser import parse_expression
    from linopy_yaml.lowering import _lower_expr

    ast = parse_expression("sum(load, over=generator)")
    assert _lower_expr(ast, dispatch_schema, "t") == Param("load")


def test_unsupported_features_rejected(dispatch_schema):
    from linopy_yaml.expression_parser import parse_expression
    from linopy_yaml.lowering import _lower_expr

    # roll() is supported since ir.Shift landed; '**' and custom Python
    # helpers remain outside the relational subset
    with pytest.raises(RelationalBuildError, match="operator '\\*\\*'"):
        _lower_expr(parse_expression("p ** 2"), dispatch_schema, "t")

    schema_dict = pyyaml.safe_load(open(DISPATCH_YAML))
    schema_dict["variables"]["p"]["binary"] = True
    schema_dict["variables"]["p"]["bounds"] = {}
    with pytest.raises(RelationalBuildError, match="binary/integer"):
        lower_program(MathSchema(**schema_dict))
