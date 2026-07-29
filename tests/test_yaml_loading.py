"""What the loader must refuse to do to a file before anyone else sees it."""

from __future__ import annotations

import pytest

import farkas as fk
from farkas._yaml import read_yaml
from farkas.schema import MathSchema

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
    expression: sum(p, over=generator) == 5
objectives:
  total:
    expression: sum(p * cost, over=generator)
"""


def _write(tmp_path, text, name='m.yaml'):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_only_true_and_false_are_booleans(tmp_path):
    """YAML 1.1 resolved these to bools, so the rows they keyed silently vanished.

    ``no`` is Norway, ``on`` is Ontario, ``y``/``n`` are perfectly ordinary
    labels. Two 1.1 coercions deliberately survive — the implicit timestamp
    and sexagesimal ints — because both interact with the unimplemented
    ``dtype: datetime``. They belong to the dtype guard in #65.
    """
    path = _write(tmp_path, 'dimensions:\n  c: {dtype: str, values: [no, se, on, off, yes, n, y]}\n')

    assert read_yaml(path)['dimensions']['c']['values'] == ['no', 'se', 'on', 'off', 'yes', 'n', 'y']


def test_real_booleans_still_parse(tmp_path):
    """The narrowed resolver must not break `binary:` / `integer:` / `convex:`."""
    path = _write(tmp_path, MODEL.replace('    bounds: {lower: 0, upper: 100}', '    binary: true\n    integer: false'))

    schema = MathSchema(**read_yaml(path))

    assert schema.variables['p'].binary is True
    assert schema.variables['p'].integer is False


def test_the_loader_yields_plain_types(tmp_path):
    """No loader wrapper may reach the schema, the AST, the plan, or the engine."""
    raw = read_yaml(_write(tmp_path, MODEL))
    assert type(raw) is dict

    schema = MathSchema(**raw)
    assert type(schema.dimensions['generator'].values) is list
    assert all(type(v) is str for v in schema.dimensions['generator'].values)
    assert type(schema.variables['p'].foreach) is list


def test_duplicate_key_is_an_error_naming_both_lines(tmp_path):
    """PyYAML keeps the last one, discarding a declaration the file contains."""
    path = _write(
        tmp_path, MODEL.replace('constraints:\n', 'constraints:\n  balance:\n    foreach: []\n    equations: []\n')
    )

    with pytest.raises(ValueError, match=r"duplicate key 'balance' .* first declared on line 12"):
        fk.check(path)


def test_duplicate_top_level_section_is_an_error(tmp_path):
    path = _write(tmp_path, MODEL + 'parameters:\n  other: {dims: [snapshot]}\n')

    with pytest.raises(ValueError, match="duplicate key 'parameters'"):
        fk.check(path)


def test_a_merge_key_override_is_not_a_duplicate(tmp_path):
    """`<<:` then a key of the same name is an override — the point of merging."""
    path = _write(
        tmp_path,
        'defaults: &d\n  foreach: [generator]\n'
        'dimensions:\n  generator: {values: [wind]}\n'
        'variables:\n  p:\n    <<: *d\n    foreach: [generator]\n',
    )

    assert read_yaml(path)['variables']['p']['foreach'] == ['generator']


def test_a_non_mapping_document_is_a_load_error(tmp_path):
    """Otherwise `MathSchema(**raw)` raises a bare TypeError about `**`."""
    for text in ('- a\n- b\n', 'just a string\n'):
        path = _write(tmp_path, text)
        with pytest.raises(ValueError, match='must be a mapping of sections'):
            fk.check(path)


def test_an_empty_file_is_an_empty_model(tmp_path):
    assert read_yaml(_write(tmp_path, '')) == {}
    assert read_yaml(_write(tmp_path, '# only a comment\n')) == {}
