"""Load errors point at a line, and the round-trip loader stays invisible."""

from __future__ import annotations

import pytest

import linopy_yaml as ly
from linopy_yaml._source import SourceMap, read_yaml
from linopy_yaml.schema import MathSchema

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


def test_a_dict_model_has_no_position_and_still_validates(tmp_path):
    """There is no file to point at; the error must survive that."""
    raw = {'dimensions': {'g': {'values': ['a']}}, 'variables': {'p': {'foreach': ['nope']}}}

    with pytest.raises(ValueError, match="undeclared dimension 'nope'"):
        ly.check(raw)


def test_the_loader_yields_plain_types(tmp_path):
    """No loader wrapper may reach the AST, the IR, or duckdb."""
    path = _write(tmp_path, MODEL)
    raw, _ = read_yaml(path)
    assert type(raw) is dict

    schema = MathSchema(**raw)
    assert type(schema.dimensions['generator'].values) is list
    assert all(type(v) is str for v in schema.dimensions['generator'].values)
    assert type(schema.variables['p'].bounds.upper) is float
    assert type(schema.variables['p'].foreach) is list


def test_only_true_and_false_are_booleans(tmp_path):
    """YAML 1.1 resolved these to bools, so the rows they keyed silently vanished.

    Two 1.1 coercions deliberately survive — the implicit timestamp and
    sexagesimal ints — because both interact with the unimplemented
    ``dtype: datetime``. They belong to the dtype guard in #65.
    """
    path = _write(tmp_path, 'dimensions:\n  c: {dtype: str, values: [no, se, on, off, yes, n, y]}\n')
    raw, _ = read_yaml(path)
    assert raw['dimensions']['c']['values'] == ['no', 'se', 'on', 'off', 'yes', 'n', 'y']


def test_real_booleans_still_parse(tmp_path):
    """The narrowed resolver must not break `binary:`/`convex:`/`active:`."""
    path = _write(tmp_path, MODEL.replace('    bounds: {lower: 0, upper: 100}', '    binary: true\n    integer: false'))
    schema = MathSchema(**read_yaml(path)[0])
    assert schema.variables['p'].binary is True
    assert schema.variables['p'].integer is False


def test_duplicate_key_is_an_error_naming_both_lines(tmp_path):
    """PyYAML keeps the last one, discarding a declaration the file contains."""
    path = _write(
        tmp_path, MODEL.replace('constraints:\n', 'constraints:\n  balance:\n    foreach: []\n    equations: []\n')
    )

    with pytest.raises(ValueError, match=r"duplicate key 'balance' .* first declared on line 12"):
        ly.check(path)


def test_duplicate_top_level_section_is_an_error(tmp_path):
    path = _write(tmp_path, MODEL + 'parameters:\n  other: {dims: [snapshot]}\n')

    with pytest.raises(ValueError, match="duplicate key 'parameters'"):
        ly.check(path)


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
