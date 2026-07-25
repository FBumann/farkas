"""The scoping divergences, checked against the oracle lane itself.

`test_resolution.py` checks the language rules. This module checks the thing
that actually mattered: that the *eager* lane now refuses what the relational
lane refuses, in the same place, for the same reason. Before resolution was a
pass, each of these built a model on one lane and raised on the other.
"""

from __future__ import annotations

import pytest
import yaml as pyyaml

import linopy_yaml as ly
from tests.oracle import compat  # skips the module without the [compat] extra

MODEL = {
    'dimensions': {'snapshot': {'dtype': 'int'}, 'generator': {'values': ['wind', 'gas']}},
    'parameters': {
        'p_max': {'dims': ['generator']},
        'cost': {'dims': ['generator']},
        'load': {'dims': ['snapshot']},
    },
    'variables': {'p': {'foreach': ['snapshot', 'generator'], 'bounds': {'lower': 0, 'upper': 'p_max'}}},
    'constraints': {
        'balance': {'foreach': ['snapshot'], 'equations': [{'expression': 'sum(p, over=generator) == load'}]}
    },
    'objectives': {'total': {'sense': 'minimize', 'equations': [{'expression': 'sum(p * cost, over=generator)'}]}},
}


@pytest.fixture
def data():
    import pandas as pd

    return {
        'p_max': pd.Series({'wind': 100.0, 'gas': 200.0}),
        'cost': pd.Series({'wind': 0.0, 'gas': 50.0}),
        'load': pd.Series([80.0] * 4, index=pd.RangeIndex(4, name='snapshot')),
    }


@pytest.fixture
def coords():
    import pandas as pd

    return {'snapshot': pd.RangeIndex(4, name='snapshot')}


def _write(tmp_path, **patch):
    import copy

    raw = copy.deepcopy(MODEL)
    for dotted, value in patch.items():
        node = raw
        *path, leaf = dotted.split('.')
        for key in path:
            node = node[key]
        node[leaf] = value
    path = tmp_path / 'm.yaml'
    path.write_text(pyyaml.safe_dump(raw))
    return path


@pytest.mark.parametrize(
    ('where', 'match', 'was'),
    [
        ('typo_name > 0', "'typo_name' not found", 'eager built 0 live variables; relational raised'),
        ('p_max > cost', 'compares two parameters', "eager compared parameters; relational compared to 'cost'"),
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
        compat.build(path, data=data, coords=coords)  # was: {was}

    with pytest.raises(ValueError, match=match):
        ly.check(path)


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


@pytest.mark.parametrize('where', ACCEPTED)
def test_both_lanes_build_the_same_model(tmp_path, data, coords, where):
    path = _write(tmp_path, **{'variables.p.where': where})

    m = compat.build(path, data=data, coords=coords)
    eager_rows = int((m.variables['p'].labels != -1).sum())
    eager_status = m.solve(solver_name='highs')[1]

    with ly.build(path, data, coords=coords) as ex:
        relational_rows = ex._con.execute('SELECT count(*) FROM var_p').fetchone()[0]
        relational_status = ex.solve().status

    # a mask that excludes snapshot 0 leaves the balance row unsatisfiable —
    # the point is that both lanes agree on *which* model they built, not that
    # every mask yields a feasible one
    assert eager_rows == relational_rows, f'{where}: {eager_rows} vs {relational_rows} variables'
    assert eager_status.lower() == relational_status.lower(), f'{where}: {eager_status} vs {relational_status}'


def test_every_resolved_predicate_is_parity_tested():
    """The guard that would have caught the DimDefined hole.

    `DimDefined` shipped in #62 lowering to `ir.Bool(True)`, which discarded
    the dimension — so unlike `DimCmp`, nothing checked it against the frame's
    dims, and a bare dimension name outside `foreach` raised eagerly and built
    relationally. No test touched it. This one fails if any resolved predicate
    is not exercised by ACCEPTED above, so a new node cannot arrive untested.
    """
    from typing import get_args

    from linopy_yaml.resolution import Namespace, where_of
    from linopy_yaml.where_parser import Comparison, ExistenceCheck, WhereNode

    unresolved = {ExistenceCheck, Comparison}  # rewritten by resolution, never evaluated
    expected = set(get_args(WhereNode)) - unresolved

    ns = Namespace((), ('p_max', 'cost', 'load'), ('snapshot', 'generator'))
    covered: set[type] = set()

    def walk(node):
        covered.add(type(node))
        for child in vars(node).values():
            if hasattr(child, '__dataclass_fields__'):
                walk(child)

    for where in ACCEPTED:
        walk(where_of(where, ns, 't'))

    missing = expected - covered
    assert not missing, f'resolved predicates with no both-lanes test: {sorted(t.__name__ for t in missing)}'


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

    m = compat.build(path, data=data, coords=coords)
    eager_status = m.solve(solver_name='highs')[1]

    with ly.build(path, data, coords=coords) as ex:
        relational_status = ex.solve().status

    assert eager_status.lower() == relational_status.lower()
