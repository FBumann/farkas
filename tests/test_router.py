"""Backend router: relational when the schema lowers, eager with a reason otherwise."""

from __future__ import annotations

import pytest
import yaml as pyyaml

from linopy_yaml.router import BackendChoice, relational_eligibility, select_backend
from linopy_yaml.schema import MathSchema


def _schema(path: str = "examples/dispatch.yaml", **overrides) -> MathSchema:
    raw = pyyaml.safe_load(open(path))
    for dotted, value in overrides.items():
        node = raw
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node[key]
        node[leaf] = value
    return MathSchema(**raw)


@pytest.mark.parametrize(
    "path",
    ["examples/dispatch.yaml", "examples/transport.yaml", "examples/storage.yaml"],
)
def test_examples_are_relational_eligible(path):
    assert relational_eligibility(_schema(path)) is None
    assert select_backend(_schema(path)) == BackendChoice("relational")


def test_binary_variable_falls_back():
    schema = _schema(**{"variables.p.binary": True, "variables.p.bounds": {}})
    choice = select_backend(schema)
    assert choice.backend == "eager"
    assert "binary" in choice.reason


def test_custom_helper_falls_back():
    import linopy_yaml  # noqa: F401 — helper registry
    from linopy_yaml.helpers import _REGISTRY

    _REGISTRY["my_helper"] = lambda x, **kw: x
    try:
        schema = _schema(
            **{
                "constraints.power_balance.equations": [
                    {"expression": "my_helper(p, over=generator) == load"}
                ]
            }
        )
        choice = select_backend(schema)
        assert choice.backend == "eager"
        assert "my_helper" in choice.reason
        assert "power_balance" in choice.reason  # reason carries context
    finally:
        del _REGISTRY["my_helper"]


def test_dimension_where_comparison_falls_back():
    schema = _schema(**{"variables.p.where": "snapshot > 2"})
    choice = select_backend(schema)
    assert choice.backend == "eager"
    assert "dimension 'snapshot'" in choice.reason
