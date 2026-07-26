"""The solve outcome, and linopy as its oracle.

`relational/status.py` copies linopy's status vocabulary spelling for
spelling, and `sinks/highs.py` copies linopy's own HiGHS mapping. Copies rot.
These tests import linopy and compare, so a divergence — ours drifting, or a
linopy release moving — fails here instead of being discovered by a user who
knows one vocabulary and is handed another.

The engine itself never imports linopy (ARCHITECTURE.md, hard rule 2). Tests
may, and this is the same oracle arrangement the differential tests use for
the math.
"""

from __future__ import annotations

import pytest

import farkas as ly
from farkas.errors import NoSolutionError
from farkas.relational.sinks.highs import _CONDITION_OF_HIGHS_STATUS
from farkas.relational.status import STATUS_TO_TERMINATION_CONDITIONS, SolveStatus

INFEASIBLE = {
    'dimensions': {'snapshot': {'dtype': 'int', 'values': [0]}},
    'parameters': {'load': {'dims': ['snapshot']}},
    'variables': {'p': {'foreach': ['snapshot'], 'bounds': {'lower': 0, 'upper': 1}}},
    'constraints': {'meet': {'foreach': ['snapshot'], 'equations': [{'expression': 'p == load'}]}},
    'objectives': {'c': {'sense': 'minimize', 'equations': [{'expression': 'p'}]}},
}


def _infeasible_sources():
    pa = pytest.importorskip('pyarrow')
    return {'load': pa.table({'snapshot': [0], 'value': [99.0]})}


# ---------------------------------------------------------------------------
# linopy as the oracle for the vocabulary
# ---------------------------------------------------------------------------


def test_the_status_rollup_matches_linopy():
    constants = pytest.importorskip('linopy.constants')
    theirs = {
        status.value: {condition.value for condition in conditions}
        for status, conditions in constants.STATUS_TO_TERMINATION_CONDITION_MAP.items()
    }
    assert {k: set(v) for k, v in STATUS_TO_TERMINATION_CONDITIONS.items()} == theirs


def test_the_highs_mapping_matches_linopy():
    """linopy builds its map inside a method, so it is read from the source.

    Brittle to a linopy refactor, deliberately: this table is a copy, and a
    copy nobody checks is a copy that rots. If linopy moves it and this stops
    finding it, the assertion below says so rather than passing vacuously.
    """
    import ast
    import inspect

    solvers = pytest.importorskip('linopy.solvers')
    tree = ast.parse(inspect.getsource(solvers))
    # several solver classes define a CONDITION_MAP; only the HiGHS one is ours
    highs = next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == 'Highs'), None)
    assert highs is not None, 'linopy no longer has a Highs solver class — re-verify the copy by hand'
    literals = [
        node.value
        for node in ast.walk(highs)
        if isinstance(node, (ast.AnnAssign, ast.Assign))
        and 'CONDITION_MAP' in ast.dump(node)
        and isinstance(node.value, ast.Dict)
    ]
    assert literals, 'linopy no longer defines CONDITION_MAP as a dict literal — re-verify the copy by hand'

    theirs = {
        key.attr: value.attr
        for key, value in zip(literals[0].keys, literals[0].values, strict=True)
        if isinstance(key, ast.Attribute) and isinstance(value, ast.Attribute)
    }
    assert theirs == _CONDITION_OF_HIGHS_STATUS


# ---------------------------------------------------------------------------
# what the two axes mean here
# ---------------------------------------------------------------------------


def test_ok_means_values_worth_reading_not_optimality():
    """A run stopped at a time limit still has an incumbent."""
    assert SolveStatus('optimal').is_ok
    assert SolveStatus('time_limit').is_ok
    assert SolveStatus('suboptimal').is_ok
    assert not SolveStatus('infeasible').is_ok
    assert not SolveStatus('unbounded').is_ok


def test_an_infeasible_solve_reports_both_axes_and_a_nan_objective():
    with ly.solve(INFEASIBLE, _infeasible_sources()) as solution:
        assert solution.status == 'warning'
        assert solution.termination_condition == 'infeasible'
        assert not solution.is_ok
        assert solution.objective != solution.objective  # nan, not 0.0


def test_reading_results_without_a_solution_raises(tmp_path):
    """HiGHS returns a full-length vector of zeros whatever the status, so
    handing it back would be indistinguishable from an answer."""
    with ly.solve(INFEASIBLE, _infeasible_sources()) as solution:
        with pytest.raises(NoSolutionError, match='infeasible'):
            solution.primal('p')
        with pytest.raises(NoSolutionError):
            solution.to_parquet(tmp_path)
