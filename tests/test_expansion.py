"""Named sub-expressions (Layer 1) and macros (Layer 2).

Both expand to core AST before backend dispatch, so one differential test at
the end proves the whole feature works identically on both backends.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import yaml as pyyaml

import linopy_yaml
from linopy_yaml.expansion import (
    _MACROS,
    parse_and_expand,
    register_macro,
    unregister_macro,
)
from linopy_yaml.expression_parser import parse_expression
from linopy_yaml.schema import MathSchema
from linopy_yaml.validation import validate_expressions


@pytest.fixture(autouse=True)
def _clean_macros():
    before = set(_MACROS)
    yield
    for name in set(_MACROS) - before:
        unregister_macro(name)


def make_schema(expressions: dict[str, str] | None = None, **overrides) -> MathSchema:
    base = {
        "dimensions": {
            "snapshot": {"dtype": "int"},
            "generator": {"values": ["wind", "gas"]},
        },
        "parameters": {
            "p_max": {"dims": ["generator"]},
            "cost": {"dims": ["generator"]},
            "load": {"dims": ["snapshot"]},
        },
        "variables": {
            "p": {
                "foreach": ["snapshot", "generator"],
                "bounds": {"lower": 0, "upper": "p_max"},
            }
        },
        "constraints": {
            "balance": {
                "foreach": ["snapshot"],
                "equations": [{"expression": "sum(p, over=generator) == load"}],
            }
        },
        "objectives": {
            "total": {
                "sense": "minimize",
                "equations": [{"expression": "sum(p * cost, over=generator)"}],
            }
        },
    }
    if expressions is not None:
        base["expressions"] = expressions
    base.update(overrides)
    return MathSchema(**base)


# ---------------------------------------------------------------------------
# Layer 1: named sub-expressions
# ---------------------------------------------------------------------------


def test_named_expression_splices():
    schema = make_schema({"gen_cost": "p * cost"})
    got = parse_and_expand("sum(gen_cost, over=generator)", schema)
    want = parse_expression("sum(p * cost, over=generator)")
    assert got == want


def test_named_expressions_nest():
    schema = make_schema(
        {"gen_cost": "p * cost", "total_cost": "sum(gen_cost, over=generator)"}
    )
    got = parse_and_expand("total_cost + 1", schema)
    want = parse_expression("sum(p * cost, over=generator) + 1")
    assert got == want


def test_named_expression_cycle_raises():
    schema = make_schema({"a": "b + 1", "b": "a + 1"})
    with pytest.raises(ValueError, match="circular expression reference: a -> b -> a"):
        parse_and_expand("a", schema)


def test_named_expression_no_comparison():
    schema = make_schema({"bad": "p == load"})
    with pytest.raises(ValueError, match="must not contain a comparison"):
        parse_and_expand("bad + 1", schema)


def test_name_collision_rejected_at_schema_level():
    with pytest.raises(ValueError, match="collides with a declared parameter"):
        make_schema({"load": "p * cost"})


def test_expand_handles_comparison_at_top():
    schema = make_schema({"total_gen": "sum(p, over=generator)"})
    got = parse_and_expand("total_gen == load", schema)
    want = parse_expression("sum(p, over=generator) == load")
    assert got == want


def test_validation_reports_bad_named_expression():
    schema = make_schema({"broken": "sum(nope, over=generator)"})
    with pytest.raises(ValueError, match="Named expression 'broken'"):
        validate_expressions(schema)


# ---------------------------------------------------------------------------
# Layer 2: macros
# ---------------------------------------------------------------------------


def test_macro_expansion():
    register_macro(
        "weighted_sum",
        "sum(array * weights, over=over)",
        args=["array", "weights"],
        kwargs=["over"],
    )
    schema = make_schema()
    got = parse_and_expand("weighted_sum(p, cost, over=generator)", schema)
    want = parse_expression("sum(p * cost, over=generator)")
    assert got == want


def test_macro_formals_shadow_model_names():
    # formal 'load' shadows the model parameter 'load' inside the body
    register_macro("double", "load + load", args=["load"])
    schema = make_schema()
    got = parse_and_expand("double(p)", schema)
    want = parse_expression("p + p")
    assert got == want


def test_macro_args_may_use_named_expressions():
    register_macro("twice", "x + x", args=["x"])
    schema = make_schema({"gen_cost": "p * cost"})
    got = parse_and_expand("twice(gen_cost)", schema)
    want = parse_expression("(p * cost) + (p * cost)")
    assert got == want


def test_macro_body_may_use_macros_and_named_expressions():
    register_macro("total", "sum(x, over=generator)", args=["x"])
    register_macro("total_cost", "total(p * cost)")
    schema = make_schema()
    got = parse_and_expand("total_cost()", schema)
    want = parse_expression("sum(p * cost, over=generator)")
    assert got == want


def test_macro_arity_errors():
    register_macro("ws", "sum(a * w, over=over)", args=["a", "w"], kwargs=["over"])
    schema = make_schema()
    with pytest.raises(ValueError, match="expects 2 positional"):
        parse_and_expand("ws(p, over=generator)", schema)
    with pytest.raises(ValueError, match="keyword argument"):
        parse_and_expand("ws(p, cost)", schema)


def test_macro_cycle_raises():
    register_macro("loop_a", "loop_b() + 1")
    register_macro("loop_b", "loop_a() + 1")
    schema = make_schema()
    with pytest.raises(ValueError, match="circular macro reference"):
        parse_and_expand("loop_a()", schema)


def test_macro_name_collisions_rejected():
    with pytest.raises(ValueError, match="conflicts with a helper"):
        register_macro("sum", "a", args=["a"])
    register_macro("once", "a", args=["a"])
    with pytest.raises(ValueError, match="already registered"):
        register_macro("once", "a", args=["a"])


def test_macro_template_validated_at_registration():
    with pytest.raises(ValueError, match="must not contain a comparison"):
        register_macro("bad", "a == b", args=["a", "b"])
    with pytest.raises(ValueError):
        register_macro("bad2", "a +* b", args=["a", "b"])


# ---------------------------------------------------------------------------
# end to end: both backends, same YAML, same answer
# ---------------------------------------------------------------------------


def test_differential_named_expression_and_macro(tmp_path):
    duckdb = pytest.importorskip("duckdb")  # noqa: F841
    highspy = pytest.importorskip("highspy")  # noqa: F841
    from linopy import Model

    from linopy_yaml.lowering import lower_program, tidy_sources
    from linopy_yaml.relational import DuckdbExecutor

    register_macro(
        "weighted_sum",
        "sum(array * weights, over=over)",
        args=["array", "weights"],
        kwargs=["over"],
    )

    yaml_text = """
dimensions:
  snapshot: {dtype: int}
  generator: {values: [wind, solar, gas]}
parameters:
  p_max: {dims: [generator]}
  cost: {dims: [generator]}
  load: {dims: [snapshot]}
expressions:
  total_generation: sum(p, over=generator)
variables:
  p:
    foreach: [snapshot, generator]
    where: "p_max > 0"
    bounds: {lower: 0, upper: p_max}
constraints:
  balance:
    foreach: [snapshot]
    equations:
      - expression: total_generation == load
objectives:
  total_cost:
    sense: minimize
    equations:
      - expression: weighted_sum(p, cost, over=generator)
"""
    yaml_file = tmp_path / "model.yaml"
    yaml_file.write_text(yaml_text)

    rng = np.random.default_rng(5)
    n_s = 24
    data = {
        "p_max": pd.Series({"wind": 100.0, "solar": 60.0, "gas": 200.0}),
        "cost": pd.Series({"wind": 1.0, "solar": 2.0, "gas": 50.0}),
        "load": pd.Series(
            (rng.uniform(0.2, 0.8, n_s) * 360.0).round(3),
            index=pd.RangeIndex(n_s, name="snapshot"),
        ),
    }
    coords = {"snapshot": pd.RangeIndex(n_s, name="snapshot")}

    m = Model.from_yaml(yaml_file, data=data, coords=coords)
    m.solve(solver_name="highs", output_flag=False)
    oracle = float(m.objective.value)
    assert np.isfinite(oracle)

    schema = MathSchema(**pyyaml.safe_load(yaml_text))
    with DuckdbExecutor(memory_limit="256MB") as ex:
        ex.build(lower_program(schema), tidy_sources(schema, data, coords))
        sol = ex.solve()
        assert sol.status == "Optimal"
        assert sol.objective == pytest.approx(oracle, rel=1e-9)


def test_python_helper_still_eager_only():
    # arbitrary-Python helpers keep working on the eager path and are
    # rejected with a clear message by the relational backend
    from linopy_yaml.lowering import _lower_expr
    from linopy_yaml.relational import RelationalBuildError

    @linopy_yaml.register("my_python_helper")
    def my_python_helper(array):  # pragma: no cover - never executed here
        return array

    try:
        schema = make_schema()
        ast = parse_and_expand("my_python_helper(p)", schema)
        with pytest.raises(RelationalBuildError, match="my_python_helper"):
            _lower_expr(ast, schema, "t")
    finally:
        from linopy_yaml.helpers import _REGISTRY

        _REGISTRY.pop("my_python_helper", None)
