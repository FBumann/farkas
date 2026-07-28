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

import farkas as fk
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
    with fk.solve(INFEASIBLE, _infeasible_sources()) as solution:
        assert solution.status == 'warning'
        assert solution.termination_condition == 'infeasible'
        assert not solution.is_ok
        assert solution.objective != solution.objective  # nan, not 0.0


def test_reading_results_without_a_solution_raises(tmp_path):
    """HiGHS returns a full-length vector of zeros whatever the status, so
    handing it back would be indistinguishable from an answer."""
    with fk.solve(INFEASIBLE, _infeasible_sources()) as solution:
        with pytest.raises(NoSolutionError, match='infeasible'):
            solution.primal('p')
        with pytest.raises(NoSolutionError):
            solution.to_parquet(tmp_path)


# ---------------------------------------------------------------------------
# solver options, and the incumbent question they make reachable
# ---------------------------------------------------------------------------


def _knapsack():
    """A MIP big enough that HiGHS does not finish it instantly."""
    import random

    random.seed(0)
    n = 60
    weights = [random.randint(10**6, 2 * 10**6) for _ in range(n)]
    model = {
        'dimensions': {'i': {'dtype': 'int', 'values': list(range(n))}, 'one': {'dtype': 'int', 'values': [0]}},
        'parameters': {'w': {'dims': ['i']}, 'cap': {'dims': ['one']}},
        'variables': {'x': {'foreach': ['i'], 'binary': True}},
        'constraints': {'budget': {'foreach': ['one'], 'equations': [{'expression': 'sum(x * w, over=i) <= cap'}]}},
        'objectives': {'o': {'sense': 'maximize', 'equations': [{'expression': 'sum(x * w, over=i)'}]}},
    }
    pa = pytest.importorskip('pyarrow')
    sources = {
        'w': pa.table({'i': list(range(n)), 'value': [float(v) for v in weights]}),
        'cap': pa.table({'one': [0], 'value': [float(sum(weights) // 2)]}),
    }
    return model, sources


def test_solver_options_reach_the_solver():
    """Forwarded verbatim, the way linopy's are. `time_limit=0` is the cheapest
    proof: without it this model solves to optimality."""
    model, sources = _knapsack()
    with fk.solve(model, sources, solver_options={'time_limit': 0.0}) as result:
        assert result.termination_condition == 'time_limit'
    with fk.solve(model, sources) as result:
        assert result.termination_condition == 'optimal'


def test_a_time_limit_with_no_incumbent_is_ok_but_unreadable():
    """The gap `is_ok` alone cannot see, and where we go beyond linopy.

    A MIP stopped before it found any feasible point rolls up to `ok` —
    linopy's `safe_get_solution` would read its zero-filled `col_value` as an
    answer. `has_primal` carries the solver's own verdict instead.
    """
    model, sources = _knapsack()
    with fk.solve(model, sources, solver_options={'time_limit': 0.0}) as result:
        assert result.is_ok  # linopy's rollup says the run was not an error
        assert not result.has_primal  # but nothing was found
        assert result.objective != result.objective  # nan
        with pytest.raises(NoSolutionError, match='time_limit'):
            result.primal('x')


def test_an_optimal_solve_is_both_ok_and_readable():
    model, sources = _knapsack()
    with fk.solve(model, sources) as result:
        assert result.is_ok
        assert result.has_primal
        assert result.primal('x')['value'].sum() > 0
