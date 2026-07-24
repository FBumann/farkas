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


def test_custom_helper_is_a_load_error_with_context():
    from linopy_yaml.helpers import _REGISTRY

    _REGISTRY['my_helper'] = lambda x, **kw: x
    try:
        schema = _schema(
            **{'constraints.power_balance.equations': [{'expression': 'my_helper(p, over=generator) == load'}]}
        )
        with pytest.raises(RelationalBuildError, match='my_helper') as exc:
            lower_program(schema)
        assert 'power_balance' in str(exc.value)  # reason carries context
    finally:
        del _REGISTRY['my_helper']


def test_dimension_where_comparison_is_a_load_error():
    schema = _schema(**{'variables.p.where': 'snapshot > 2'})
    with pytest.raises(RelationalBuildError, match="dimension 'snapshot'"):
        lower_program(schema)
