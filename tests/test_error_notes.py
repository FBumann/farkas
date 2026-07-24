"""Tests for Exception.add_note() context attached by from_yaml/extend/builder."""

from __future__ import annotations

import pandas as pd
import pytest

from tests.oracle import compat, linopy


def _has_note(exc: BaseException, substring: str) -> bool:
    return any(substring in n for n in getattr(exc, '__notes__', []))


def test_from_yaml_malformed_where_fails_at_load(tmp_path):
    """A malformed where string is caught by load-time validation."""
    bad = tmp_path / 'bad.yaml'
    bad.write_text(
        'dimensions:\n'
        '  g: {values: [a]}\n'
        'variables:\n'
        '  p:\n'
        '    foreach: [g]\n'
        "    where: '<<<'\n"  # malformed where string
    )

    with pytest.raises(ValueError, match='Failed to parse where string') as ei:
        compat.build(bad)

    assert "Variable 'p'" in str(ei.value)
    assert _has_note(ei.value, f"while loading YAML '{bad}'")


def test_from_yaml_missing_comparison_fails_at_load(tmp_path):
    """A constraint expression without a comparison is caught at load time."""
    bad = tmp_path / 'bad.yaml'
    bad.write_text(
        'dimensions:\n'
        '  g: {values: [a]}\n'
        'variables:\n'
        '  p:\n'
        '    foreach: [g]\n'
        'constraints:\n'
        '  c:\n'
        '    foreach: [g]\n'
        '    equations:\n'
        "      - expression: 'p + 1'\n"  # no comparison operator
    )

    with pytest.raises(ValueError, match='exactly one comparison') as ei:
        compat.build(bad)

    assert "Constraint 'c'" in str(ei.value)
    assert _has_note(ei.value, f"while loading YAML '{bad}'")


def test_from_yaml_objective_comparison_fails_at_load(tmp_path):
    """An objective expression with a comparison is caught at load time."""
    bad = tmp_path / 'bad.yaml'
    bad.write_text(
        'dimensions:\n'
        '  g: {values: [a]}\n'
        'variables:\n'
        '  p:\n'
        '    foreach: [g]\n'
        'objectives:\n'
        '  obj:\n'
        '    equations:\n'
        "      - expression: 'p == 1'\n"  # objectives forbid comparison operators
    )

    with pytest.raises(ValueError, match='must not contain a comparison') as ei:
        compat.build(bad)

    assert "Objective 'obj'" in str(ei.value)
    assert _has_note(ei.value, f"while loading YAML '{bad}'")


def test_build_failure_attaches_constraint_note(tmp_path):
    """Errors validation cannot catch still carry build-phase notes."""
    bad = tmp_path / 'bad.yaml'
    bad.write_text(
        'dimensions:\n'
        '  g: {values: [a]}\n'
        'variables:\n'
        '  p:\n'
        '    foreach: [g]\n'
        'constraints:\n'
        '  c:\n'
        '    foreach: [g]\n'
        '    equations:\n'
        "      - expression: '1 <= 2'\n"  # valid syntax, but no variable on the LHS
    )

    with pytest.raises(TypeError) as ei:
        compat.build(bad)

    assert _has_note(ei.value, "while building constraint 'c'")
    assert _has_note(ei.value, f"while loading YAML '{bad}'")


def test_extend_attaches_path_note(tmp_path):
    """A failure inside extend() carries the extension YAML path."""
    model = linopy.Model()
    model.add_variables(name='p', coords=[pd.Index([0, 1, 2, 3], name='time')])

    ext = tmp_path / 'ext.yaml'
    ext.write_text(
        'dimensions:\n  time:\n    values: [a, b]\n'  # mismatched values trigger the existing-coords check
    )

    with pytest.raises(ValueError) as ei:
        compat.extend(model, ext)

    assert _has_note(ei.value, f"while extending with YAML '{ext}'")
