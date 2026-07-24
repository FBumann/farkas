"""The compat shim: YAML math onto a linopy.Model, as a pure producer.

No accessor, no session, no state on the model — a file's meaning must not
depend on what was loaded before it (ARCHITECTURE.md, hard rule 5).
"""

from __future__ import annotations

import pandas as pd
import pytest

from tests.oracle import compat, linopy


def test_nothing_is_patched_onto_linopy_model():
    """Importing compat must not touch linopy.Model."""
    assert not hasattr(linopy.Model, 'from_yaml')
    assert not hasattr(linopy.Model, 'yaml')


def test_extend_rejects_mismatched_dim_values(tmp_path):
    """Extension declaring values: for an existing dim must match exactly."""
    m = linopy.Model()
    m.add_variables(name='p', coords=[pd.Index([0, 1, 2, 3], name='time')])

    ext = tmp_path / 'ext.yaml'
    ext.write_text('dimensions:\n  time:\n    values: [a, b]\n')

    with pytest.raises(ValueError, match='differ from the existing model'):
        compat.extend(m, ext)


def test_extend_accepts_matching_dim_values(tmp_path):
    """Extension may redeclare values: as long as they match exactly."""
    m = linopy.Model()
    m.add_variables(name='p', coords=[pd.Index(['wind', 'solar'], name='generator')])

    ext = tmp_path / 'ext.yaml'
    ext.write_text('dimensions:\n  generator:\n    values: [wind, solar]\n')

    compat.extend(m, ext)  # must not raise


def test_infer_coords_unions_across_variables():
    """_infer_coords unions per-dim coordinates across all model variables."""
    m = linopy.Model()
    m.add_variables(name='a', coords=[pd.Index(['wind', 'solar'], name='generator')])
    m.add_variables(name='b', coords=[pd.Index(['wind', 'gas'], name='generator')])

    inferred = compat._infer_coords(m)
    assert 'generator' in inferred
    assert set(inferred['generator']) == {'wind', 'solar', 'gas'}


def test_extend_uses_inferred_coords_when_yaml_omits_values(tmp_path):
    """Extension YAML may omit values: for dims already on the model."""
    m = linopy.Model()
    m.add_variables(name='p', coords=[pd.Index(['wind', 'solar'], name='generator')])

    ext = tmp_path / 'ext.yaml'
    ext.write_text(
        'dimensions:\n'
        '  generator: {}\n'
        'parameters:\n'
        '  cap:\n'
        '    dims: [generator]\n'
        'constraints:\n'
        '  limit:\n'
        '    foreach: [generator]\n'
        '    equations:\n'
        '      - expression: p <= cap\n'
    )

    compat.extend(m, ext, data={'cap': pd.Series({'wind': 1.0, 'solar': 2.0})})

    assert 'limit' in m.constraints


def test_extend_rejects_yaml_values_that_disagree_with_inferred(tmp_path):
    """Extension values: must match inferred coords, not just declared ones."""
    m = linopy.Model()
    m.add_variables(name='p', coords=[pd.Index(['wind', 'solar'], name='generator')])

    ext = tmp_path / 'ext.yaml'
    ext.write_text('dimensions:\n  generator:\n    values: [wind, gas]\n')

    with pytest.raises(ValueError, match='differ from the existing model'):
        compat.extend(m, ext)


def test_extend_coords_kwarg_overrides_inferred(tmp_path):
    """coords= kwarg to extend() wins over inference from model variables."""
    m = linopy.Model()
    m.add_variables(name='p', coords=[pd.Index(['wind', 'solar'], name='generator')])

    ext = tmp_path / 'ext.yaml'
    ext.write_text('dimensions:\n  generator: {}\nparameters:\n  cap:\n    dims: [generator]\n')

    compat.extend(
        m,
        ext,
        data={'cap': pd.Series({'wind': 1.0, 'gas': 3.0})},
        coords={'generator': ['wind', 'gas']},
    )  # must not raise: the override, not inference, defines the dim here


def test_extend_is_stateless(tmp_path):
    """A second YAML cannot lean on the first: every file declares what it uses.

    This is hard rule 5 — no Python-side state may change what a file means.
    """
    m = linopy.Model()

    first = tmp_path / 'first.yaml'
    first.write_text(
        'dimensions:\n'
        '  generator:\n'
        '    values: [wind, solar]\n'
        'parameters:\n'
        '  cap:\n'
        '    dims: [generator]\n'
        'variables:\n'
        '  q:\n'
        '    foreach: [generator]\n'
        'constraints:\n'
        '  limit:\n'
        '    foreach: [generator]\n'
        '    equations:\n'
        '      - expression: q <= cap\n'
    )
    compat.extend(m, first, data={'cap': pd.Series({'wind': 1.0, 'solar': 2.0})})

    # 'q' is a model variable, so the second file may reference it. 'cap' is
    # not redeclared here — and must therefore be unknown.
    second = tmp_path / 'second.yaml'
    second.write_text(
        'dimensions:\n'
        '  generator: {}\n'
        'constraints:\n'
        '  limit2:\n'
        '    foreach: [generator]\n'
        '    equations:\n'
        '      - expression: q <= cap\n'
    )
    with pytest.raises(ValueError, match="'cap' not found"):
        compat.extend(m, second)
