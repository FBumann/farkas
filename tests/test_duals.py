"""Duals: the shadow price read-back, and the two models that have none.

A nodal balance's dual is the price at that node, which is why this is a
headline output rather than a diagnostic — and why the differential test below
compares *values*, not just presence: a sign convention that disagreed with
linopy would be a silently wrong answer of exactly the kind the two-lane claim
exists to catch.

The other half of the feature is the refusals. A MILP has no dual solution and
an infeasible solve has no valid one, and in both cases returning zeros would
look like an answer.
"""

from __future__ import annotations

import numpy as np
import pytest

import farkas as fk
from farkas.errors import LinopyYamlError, NoSolutionError
from tests.differential import differential
from tests.oracle import pd  # through the guard: a bare import would beat it
from tests.test_milp import COMMITMENT_YAML

DUAL_RTOL = 1e-9


def test_dual_matches_the_eager_lane(dispatch_yaml, dispatch_inputs):
    """The price at each snapshot, both lanes, same sign and same magnitude."""
    data, coords = dispatch_inputs

    with differential(dispatch_yaml, data, coords) as run:
        got = run.result.dual('power_balance')

        assert got.columns == ['snapshot', 'value']
        assert got.height == len(coords['snapshot'])

        oracle = run.model.constraints['power_balance'].dual
        expected = pd.Series(np.asarray(oracle), index=np.asarray(oracle.indexes['snapshot']))
        actual = got.sort('snapshot')['value'].to_numpy()

        assert actual == pytest.approx(expected.sort_index().to_numpy(), rel=DUAL_RTOL)

        # a price is what the marginal unit costs: with distinct costs and a
        # binding balance, every dual sits on one of the generator costs.
        assert set(np.round(actual, 6)) <= set(np.round(data['cost'].to_numpy(), 6))


def test_dual_respects_the_where_mask(dispatch_yaml, dispatch_inputs):
    """Duals are a label join, so a masked row is absent — never a zero."""
    data, coords = dispatch_inputs
    trimmed = dict(coords, snapshot=coords['snapshot'][:12])
    data = dict(data, load=data['load'].iloc[:12])

    with differential(dispatch_yaml, data, trimmed) as run:
        got = run.result.dual('power_balance')
        assert got.height == 12
        assert sorted(got['snapshot'].to_list()) == list(range(12))


def test_milp_refuses_duals_and_names_the_variable(commitment_inputs):
    """Integrality is decidable from the program, so the message says which.

    **A deliberate divergence from the oracle, not an oversight.** Asked for
    the duals of this same model, linopy hands back an array of zeros — the
    ``dual`` entry exists and every value is ``0.0`` — which is a plausible
    number with no signal attached, the ``bug:silent`` class. (On an
    *unsolved* model the two lanes agree that it must raise: linopy through
    its ``has_optimized_model`` gate, we through ``_require_solution`` — see
    the infeasible test below.)

    Hard rule 3 governs the *language*, and both lanes still accept this
    model and agree on its objective; what differs is a post-solve read-back
    that has no defined answer. #78 fixed the direction: a model with any
    binary or integer variable must raise, naming the reason, not return
    zeros. Parity here would be parity with the bug.
    """
    data, coords = commitment_inputs

    with differential(COMMITMENT_YAML, data, coords) as run:
        with pytest.raises(LinopyYamlError) as excinfo:
            run.result.dual('balance')

        message = str(excinfo.value)
        assert 'mixed-integer' in message
        assert "'u'" in message, 'the refusal should name the non-continuous variable'
        # the primal is still perfectly readable — only duals are undefined
        assert len(run.result.primal('u')) > 0


def test_infeasible_solve_refuses_duals(dispatch_yaml, dispatch_inputs):
    """No values at all is the *other* refusal — the one `primal` shares.

    `dual` goes through `_require_solution` before it looks at duals, so an
    infeasible solve raises `NoSolutionError` exactly as `primal` does rather
    than reporting the narrower "this model has no duals".
    """
    data, coords = dispatch_inputs
    # demand every generator together cannot meet, at every snapshot
    data = dict(data, load=pd.Series(1e6, index=coords['snapshot']))

    with fk.solve(dispatch_yaml, data, coords=coords) as result:
        assert not result.has_primal
        assert result.termination_condition == 'infeasible'

        with pytest.raises(NoSolutionError, match='cannot read the dual'):
            result.dual('power_balance')
        # …and the same refusal, same class, for the primal
        with pytest.raises(NoSolutionError):
            result.primal('p')


def test_unknown_constraint_is_a_keyerror(dispatch_yaml, dispatch_inputs):
    data, coords = dispatch_inputs

    with (
        differential(dispatch_yaml, data, coords) as run,
        pytest.raises(KeyError, match='unknown constraint'),
    ):
        run.result.dual('power_balnce')
