"""Load errors point at the line they came from."""

from __future__ import annotations

import pytest

import linopy_yaml as ly
from linopy_yaml._yaml import SourceMap, read_yaml

MODEL = """dimensions:
  snapshot: {dtype: int, values: [0, 1]}
  generator: {values: [wind, gas]}
parameters:
  cost: {dims: [generator]}
variables:
  p:
    foreach: [snapshot, generator]
    where: "cost > 0"
    bounds: {lower: 0, upper: 100}
constraints:
  balance:
    foreach: [snapshot]
    equations:
      - expression: sum(p, over=generator) == 5
objectives:
  total:
    equations:
      - expression: sum(p * cost, over=generator)
"""


def _write(tmp_path, text, name='m.yaml'):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_expression_error_names_the_equation_line(tmp_path):
    """The typo is on line 15; the message has to say so."""
    path = _write(tmp_path, MODEL.replace('over=generator) == 5', 'over=generatr) == 5'))

    with pytest.raises(ValueError) as ei:
        ly.check(path)

    assert f'{path}:15' in str(ei.value)
    assert "Constraint 'balance', equation 0" in str(ei.value)


def test_where_error_names_the_where_line(tmp_path):
    path = _write(tmp_path, MODEL.replace('where: "cost > 0"', 'where: "nosuch > 0"'))

    with pytest.raises(ValueError) as ei:
        ly.check(path)

    assert f'{path}:9' in str(ei.value)
    assert "Variable 'p'" in str(ei.value)


def test_schema_shape_error_names_the_line(tmp_path):
    """Shape errors come from pydantic, so they travel a different path."""
    path = _write(tmp_path, MODEL.replace('foreach: [snapshot, generator]', 'foreach: snapshot'))

    with pytest.raises(ValueError) as ei:
        ly.check(path)

    assert f'{path}:8' in str(ei.value)
    assert 'variables.p.foreach' in str(ei.value)


def test_a_dict_model_has_no_position_and_still_validates():
    """There is no file to point at; the error must survive that."""
    raw = {'dimensions': {'g': {'values': ['a']}}, 'variables': {'p': {'foreach': ['nope']}}}

    with pytest.raises(ValueError, match="undeclared dimension 'nope'"):
        ly.check(raw)


def test_sourcemap_degrades_to_the_nearest_resolvable_ancestor(tmp_path):
    path = _write(tmp_path, MODEL)
    _, source = read_yaml(path)

    assert source.line('variables', 'p', 'where') == 9
    # an absent key falls back to the declaration that would have held it
    assert source.line('variables', 'p', 'no_such_key') == 7
    assert source.line('no_such_section') is None
    assert source.at('no_such_section') == str(path)


def test_sourcemap_none_renders_nothing():
    none = SourceMap.none()
    assert none.at('variables', 'p') == ''
    assert none.where('variables', 'p') == ''
    assert none.line('variables', 'p') is None


def test_a_merged_key_falls_back_to_its_declaration(tmp_path):
    """`<<:` splices keys in at construction; they have no line of their own."""
    path = _write(
        tmp_path,
        'defaults: &d\n  foreach: [generator]\n'
        'dimensions:\n  generator: {values: [wind]}\n'
        'variables:\n  p:\n    <<: *d\n',
    )
    _, source = read_yaml(path)

    assert source.line('variables', 'p', 'foreach') == source.line('variables', 'p')
