"""The scoping divergences, checked against the oracle lane itself.

`test_resolution.py` checks the language rules. This module checks the thing
that actually mattered: that the *eager* lane now refuses what the relational
lane refuses, in the same place, for the same reason. Before resolution was a
pass, each of these built a model on one lane and raised on the other.
"""

from __future__ import annotations

import polars as pl
import pytest
import yaml as pyyaml

import lpspec as lps
from tests.conftest import DISPATCH_MODEL, override
from tests.oracle import lpspec_linopy, pd  # skips the module without the [linopy] extra


@pytest.fixture
def data():
    return {
        'p_max': pd.Series({'wind': 100.0, 'gas': 200.0}),
        'cost': pd.Series({'wind': 0.0, 'gas': 50.0}),
        'load': pd.Series([80.0] * 4, index=pd.RangeIndex(4, name='snapshot')),
    }


@pytest.fixture
def coords():
    return {'snapshot': pd.RangeIndex(4, name='snapshot')}


def _write(tmp_path, **patch):
    """The eager lane only takes a path, so a varied model has to hit disk."""
    path = tmp_path / 'm.yaml'
    path.write_text(pyyaml.safe_dump(override(DISPATCH_MODEL, **patch)))
    return path


@pytest.mark.parametrize(
    ('where', 'match', 'was'),
    [
        ('typo_name > 0', "'typo_name' not found", 'eager built 0 live variables; relational raised'),
        ('p_max > cost', 'compares two parameters', "eager compared parameters; relational compared to 'cost'"),
        (
            'generator == snapshot',
            'compares against dimension',
            "both read the RHS as the string 'snapshot' and built the block empty",
        ),
        ('nonexistent', "'nonexistent' not found", 'eager masked everything out; relational raised'),
        (
            'snapshot',
            'bare dimension name is true at every coordinate',
            'eager raised only when the dim was outside foreach; relational always built',
        ),
    ],
)
def test_both_lanes_refuse_the_same_where(tmp_path, data, coords, where, match, was):
    path = _write(tmp_path, **{'variables.p.where': where})

    with pytest.raises(ValueError, match=match):
        lpspec_linopy.build(path, data=data, coords=coords)  # was: {was}

    with pytest.raises(ValueError, match=match):
        lps.check(path)


#: Where-strings that must build *identically* on both lanes. Chosen to cover
#: every resolved predicate type — see the exhaustiveness test below. The dim
#: comparisons are deliberately always-true: a mask that removes every variable
#: from a constraint row exposes a separate divergence, pinned below.
ACCEPTED = [
    'True',
    'p_max',
    'p_max > 0',
    'snapshot >= 0',
    'NOT p_max > 150',
    'p_max > 0 AND snapshot >= 0',
    'p_max > 0 OR snapshot >= 0',
]

#: Predicates this sweep cannot host, with where they are checked instead. The
#: sweep masks ``variables.p.where`` on the dispatch model, and a bare variable
#: name fits neither slot: in a variable's own where it is a self-reference
#: (rejected at load), and on ``balance`` it spans a dim the constraint does not
#: (a DimensionError, correctly — reducing it needs ROADMAP Track 1 item 6).
#: Mapped rather than skipped so the coverage guard below still names a test.
COVERED_ELSEWHERE = {
    'VariableDefinedNode': ('tests/test_relational.py::test_a_bare_variable_name_in_a_where_asks_whether_it_exists'),
}


@pytest.mark.parametrize('where', ACCEPTED)
def test_both_lanes_build_the_same_model(tmp_path, data, coords, where):
    path = _write(tmp_path, **{'variables.p.where': where})

    m = lpspec_linopy.build(path, data=data, coords=coords)
    eager_rows = int((m.variables['p'].labels != -1).sum())
    eager_status = m.solve(solver_name='highs')[1]

    with lps.build(path, data, coords=coords) as ex:
        relational_rows = ex._variables['p'].select(pl.len()).collect().item()
        relational_status = ex.solve().termination_condition

    # a mask that excludes snapshot 0 leaves the balance row unsatisfiable —
    # the point is that both lanes agree on *which* model they built, not that
    # every mask yields a feasible one
    assert eager_rows == relational_rows, f'{where}: {eager_rows} vs {relational_rows} variables'
    assert eager_status == relational_status, f'{where}: {eager_status} vs {relational_status}'


def test_every_resolved_predicate_is_parity_tested():
    """The guard that would have caught the DimDefined hole.

    `DimDefined` shipped in #62 lowering to `plan.BooleanConstant(True)`, which discarded
    the dimension — so unlike `DimensionComparisonNode`, nothing checked it against the frame's
    dims, and a bare dimension name outside `foreach` raised eagerly and built
    relationally. No test touched it. This one fails if any resolved predicate
    is not exercised by ACCEPTED above, so a new node cannot arrive untested.
    """
    from typing import get_args

    from lpspec.resolution import Namespace, where_of
    from lpspec.where_parser import UnresolvedComparisonNode, UnresolvedNameNode, WhereNode

    unresolved = {UnresolvedNameNode, UnresolvedComparisonNode}  # rewritten by resolution, never evaluated
    expected = set(get_args(WhereNode)) - unresolved

    ns = Namespace(('p',), ('p_max', 'cost', 'load'), ('snapshot', 'generator'))
    covered: set[type] = set()

    def walk(node):
        covered.add(type(node))
        for child in vars(node).values():
            if hasattr(child, '__dataclass_fields__'):
                walk(child)

    for where in ACCEPTED:
        walk(where_of(where, ns, 't'))
    covered |= {t for t in expected if t.__name__ in COVERED_ELSEWHERE}

    missing = expected - covered
    assert not missing, (
        f'resolved predicates with no both-lanes test: {sorted(t.__name__ for t in missing)}. '
        f'Add it to ACCEPTED, or to COVERED_ELSEWHERE naming the test that does cover it.'
    )


@pytest.mark.xfail(strict=True, reason='orphaned constraint rows: the lanes disagree — see the docstring')
def test_a_constraint_row_left_with_no_variables(tmp_path, data, coords):
    """A masked *variable* can orphan an unmasked *constraint* row, and the
    lanes then disagree about what the model even is.

    `where: "snapshot > 0"` on `p` leaves `power_balance` at snapshot 0 with no
    terms. Both lanes build four constraint labels, but linopy hands the solver
    three — the orphaned row is dropped, so a constraint the file declares goes
    unenforced and the model solves `optimal`. The relational lane keeps the
    row as `0 == 80` and reports `Infeasible`.

    Unrelated to name resolution; found by the parity sweep above. The
    relational reading looks right (the file says the balance holds at every
    snapshot), but which lane changes is a language decision, so this is pinned
    rather than fixed here.
    """
    path = _write(tmp_path, **{'variables.p.where': 'snapshot > 0'})

    m = lpspec_linopy.build(path, data=data, coords=coords)
    eager_status = m.solve(solver_name='highs')[1]

    with lps.build(path, data, coords=coords) as ex:
        relational_status = ex.solve().termination_condition

    assert eager_status == relational_status


BOOL_MASK_MODEL = {
    'dimensions': {'t': {'dtype': 'int', 'values': [0, 1, 2]}},
    'parameters': {'active': {'dims': ['t'], 'dtype': 'bool'}, 'cap': {'dims': ['t']}},
    'variables': {'x': {'foreach': ['t'], 'bounds': {'lower': 0, 'upper': 'cap'}}},
    'constraints': {'floor': {'foreach': ['t'], 'expression': 'x >= cap', 'where': 'active'}},
    'objectives': {'total': {'sense': 'minimize', 'expression': 'sum(x, over=t)'}},
}


def test_a_bool_parameter_is_a_mask_on_both_lanes(tmp_path):
    """A bool parameter reads as its own value: true masks in, false masks out,
    and an absent row masks out. Was: the relational lane raised
    `isfinite(BOOLEAN)` at build, and the eager lane read false as true.
    """
    import pandas as pd

    path = tmp_path / 'm.yaml'
    path.write_text(pyyaml.safe_dump(BOOL_MASK_MODEL))
    data = {
        'active': pd.Series({0: True, 1: False}),
        'cap': pd.Series({0: 1.0, 1: 1.0, 2: 1.0}),
    }

    m = lpspec_linopy.build(path, data=data)
    m.solve(solver_name='highs')
    eager = float(m.objective.value)

    with lps.solve(path, data) as result:
        relational = result.objective

    assert eager == relational == 1.0
