"""The streaming language boundary: out-of-subset constructs are load errors.

There is no runtime fallback — the streaming subset IS the language
(ARCHITECTURE.md). The eager builder survives only as the opt-in
compatibility layer (`linopy_yaml.compat`) and the differential oracle.
Errors must carry the construct and its context, verbatim.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml as pyyaml

from linopy_yaml.lowering import lower_program
from linopy_yaml.relational import RelationalBuildError
from linopy_yaml.schema import MathSchema


def _schema(path: str = 'examples/dispatch.yaml', **overrides) -> MathSchema:
    raw = pyyaml.safe_load(Path(path).read_text())
    for dotted, value in overrides.items():
        node = raw
        *parents, leaf = dotted.split('.')
        for key in parents:
            node = node[key]
        node[leaf] = value
    return MathSchema(**raw)


@pytest.mark.parametrize(
    'path',
    ['examples/dispatch.yaml', 'examples/transport.yaml', 'examples/storage.yaml'],
)
def test_examples_are_inside_the_language(path):
    lower_program(_schema(path))  # must not raise


def test_binary_variable_is_inside_the_language():
    schema = _schema(**{'variables.p.binary': True, 'variables.p.bounds': {}})
    lower_program(schema)


def test_power_operator_is_a_load_error():
    schema = _schema(**{'constraints.power_balance.equations': [{'expression': 'sum(p ** 2, over=generator) == load'}]})
    with pytest.raises(RelationalBuildError, match=r"operator '\*\*'"):
        lower_program(schema)


def test_unknown_helper_is_a_load_error_with_context():
    schema = _schema(
        **{'constraints.power_balance.equations': [{'expression': 'my_helper(p, over=generator) == load'}]}
    )
    with pytest.raises(RelationalBuildError, match='my_helper') as exc:
        lower_program(schema)
    reason = str(exc.value)
    assert 'power_balance' in reason  # reason carries context
    assert 'escape' in reason  # ...and the rewrite, not a pointer to another lane
    assert 'eager' not in reason.lower()


def test_dimension_where_comparison_is_inside_the_language():
    """ROADMAP 5b: both lanes accept `where: "snapshot > 2"`."""
    lower_program(_schema(**{'variables.p.where': 'snapshot > 2'}))  # must not raise


def test_degree_two_is_a_load_error_not_a_build_error():
    """`check()` must catch degree 2 — it is the first clause of the ceiling.

    The affine guard used to live only in the executor, so it needed data
    bound: `ly.check()` accepted `sum(p * p, over=g)` and the model only blew
    up at build time. That made check() useless as the CI verb for exactly the
    rule it should enforce first.
    """
    schema = _schema(**{'objectives.total_cost.equations': [{'expression': 'sum(p * p, over=generator)'}]})
    with pytest.raises(RelationalBuildError, match='degree 2'):
        lower_program(schema)


def test_variable_divisor_is_a_load_error():
    schema = _schema(**{'objectives.total_cost.equations': [{'expression': 'sum(cost / p, over=generator)'}]})
    with pytest.raises(RelationalBuildError, match='divisor contains variables'):
        lower_program(schema)


def test_affine_products_still_lower():
    lower_program(_schema(**{'objectives.total_cost.equations': [{'expression': 'sum(p * cost, over=generator)'}]}))
