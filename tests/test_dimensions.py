"""Dim sets are a type system, checked before any data is bound.

Every case here used to build a model and solve it — wrongly, or larger than
the file reads as. None of them needs data to be caught.
"""

from __future__ import annotations

import copy

import pytest
import yaml as pyyaml

from linopy_yaml.dimensions import DimError, check_schema, dims_of
from linopy_yaml.resolution import Namespace, expression_of
from linopy_yaml.schema import MathSchema

BASE = {
    'dimensions': {
        'snapshot': {'dtype': 'int'},
        'generator': {'values': ['wind', 'gas']},
        'bus': {'values': ['n', 's']},
    },
    'parameters': {
        'p_max': {'dims': ['generator']},
        'cost': {'dims': ['generator']},
        'gen_bus': {'dims': ['generator'], 'dtype': 'str'},
        'load': {'dims': ['snapshot', 'bus']},
    },
    'variables': {'p': {'foreach': ['snapshot', 'generator'], 'bounds': {'lower': 0, 'upper': 'p_max'}}},
    'constraints': {
        'balance': {
            'foreach': ['snapshot', 'bus'],
            'equations': [{'expression': 'group_sum(p, gen_bus, into=bus) == load'}],
        }
    },
    'objectives': {'total': {'sense': 'minimize', 'equations': [{'expression': 'sum(p * cost, over=generator)'}]}},
}


def _schema(**overrides) -> MathSchema:
    raw = copy.deepcopy(BASE)
    for dotted, value in overrides.items():
        node = raw
        *path, leaf = dotted.split('.')
        for key in path:
            node = node.setdefault(key, {})
        node[leaf] = value
    return MathSchema(**raw)


def _dims(expr: str, schema: MathSchema | None = None) -> frozenset[str]:
    s = schema or _schema()
    return dims_of(expression_of(expr, s, Namespace.of(s), 't'), s, 't')


def test_the_base_model_typechecks():
    check_schema(_schema())


# ---------------------------------------------------------------------------
# the rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('expr', 'expected'),
    [
        ('7', set()),
        ('cost', {'generator'}),
        ('p', {'snapshot', 'generator'}),
        ('-p', {'snapshot', 'generator'}),
        ('p * cost', {'snapshot', 'generator'}),
        ('sum(p, over=generator)', {'snapshot'}),
        ('sum(p * cost, over=generator)', {'snapshot'}),
        ('group_sum(p, gen_bus, into=bus)', {'snapshot', 'bus'}),
        ('roll(p, snapshot=1)', {'snapshot', 'generator'}),
    ],
)
def test_dim_inference(expr, expected):
    assert _dims(expr) == expected


def test_sum_over_an_absent_dim_is_an_error_not_a_noop():
    """SPEC §7.1 used to return the array unchanged. `sum(p, over=bus)` then
    built and solved a model that silently never summed anything."""
    with pytest.raises(DimError, match='sum\\(over=bus\\) but the expression has dims'):
        _dims('sum(p, over=bus)')


def test_group_sum_requires_the_mapping_dim():
    with pytest.raises(DimError, match="mapping 'gen_bus' has dims"):
        _dims('group_sum(load, gen_bus, into=bus)')


def test_roll_requires_the_dim():
    with pytest.raises(DimError, match="roll\\(\\) along 'snapshot'"):
        _dims('roll(cost, snapshot=1)')


def test_incomparable_dims_are_rejected():
    """The subset rule: neither operand's dims contain the other's, so the
    result would carry both and build a bigger model than either."""
    with pytest.raises(DimError, match='cannot combine dims'):
        _dims('cost + load')


def test_broadcast_is_legal_when_one_side_contains_the_other():
    assert _dims('p * cost') == {'snapshot', 'generator'}
    assert _dims('p + 1') == {'snapshot', 'generator'}


# ---------------------------------------------------------------------------
# declaration-level rules
# ---------------------------------------------------------------------------


def test_stray_dim_in_a_constraint_is_rejected():
    """The rule that matters most: a dim the foreach does not declare
    multiplies the rows this constraint builds."""
    schema = _schema(**{'constraints.stray': {'foreach': ['snapshot'], 'equations': [{'expression': 'p <= p_max'}]}})
    with pytest.raises(DimError, match=r"carries dims \['generator'\] that are not in foreach"):
        check_schema(schema)


def test_foreach_dim_the_equation_never_uses_is_rejected():
    schema = _schema(
        **{
            'constraints.unused': {
                'foreach': ['snapshot', 'generator', 'bus'],
                'equations': [{'expression': 'p <= p_max'}],
            }
        }
    )
    with pytest.raises(DimError, match=r"does not carry \['bus'\]"):
        check_schema(schema)


def test_where_dim_outside_the_frame_is_rejected():
    """SPEC §6.3 documented an `any()` reduction here — a mask that fails
    *open*, silently including everything."""
    schema = _schema(**{'variables.cap': {'foreach': ['generator'], 'where': 'load > 0'}})
    with pytest.raises(DimError, match=r"where-parameter 'load' has dims \['bus', 'snapshot'\]"):
        check_schema(schema)


def test_where_comparison_on_a_dim_outside_the_frame_is_rejected():
    schema = _schema(**{'variables.cap': {'foreach': ['generator'], 'where': 'snapshot > 0'}})
    with pytest.raises(DimError, match="where-comparison on dimension 'snapshot'"):
        check_schema(schema)


def test_bound_parameter_dim_outside_foreach_is_rejected():
    schema = _schema(**{'variables.cap': {'foreach': ['generator'], 'bounds': {'lower': 0, 'upper': 'load'}}})
    with pytest.raises(DimError, match=r"bounds.upper parameter 'load' has dims \['bus', 'snapshot'\]"):
        check_schema(schema)


def test_checking_needs_no_data():
    """The whole point: every rule above is decided from declarations alone,
    so `ly.check()` catches them in CI with no sources bound."""
    import linopy_yaml as ly

    raw = copy.deepcopy(BASE)
    raw['constraints']['stray'] = {'foreach': ['snapshot'], 'equations': [{'expression': 'p <= p_max'}]}
    with pytest.raises(DimError):
        ly.check(raw)


def test_shipped_examples_typecheck(tmp_path):
    from pathlib import Path

    for path in sorted(Path('examples').glob('*.yaml')):
        schema = MathSchema(**pyyaml.safe_load(path.read_text()))
        check_schema(schema)
