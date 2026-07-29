"""The algebraic laws the language promises — and the ones it deliberately breaks.

linopy's v1 convention ships formal law tests for the same reason this file
exists: an arithmetic convention is a set of *equalities between spellings*, and
nothing else in a test suite checks those. A model can build, solve, and agree
across both lanes while `a + b` and `b + a` quietly mean different things.

Two kinds of case here, and the second is the point:

**Laws** — spellings that must produce the same model. Each is solved through
``differential`` (eager lane, relational lane, and the LP file re-solve), so a
law holding is six numbers agreeing rather than two.

**Non-laws** — spellings that are equal in ordinary algebra and are *not* equal
here, because absence is a first-class state. These are asserted to differ, with
the values written down. They are the ones worth having: ``sum(a + b)`` versus
``sum(a) + sum(b)`` diverged silently by 40% on one lane for as long as the
oracle was blind to it (#311), and no law-shaped test would have caught it —
only a test that says *these two are supposed to disagree, and by exactly this
much*.

The fixture keeps one masked variable (``y``, absent at ``f=b``) and one total
one (``x``), because every interesting law is conditional on whether absence is
in play. Laws are therefore checked twice where it matters: once over ``x``
alone, where they hold, and once over ``y``, where some of them stop.
"""

from __future__ import annotations

import pytest

from tests.differential import RTOL, differential
from tests.oracle import pd

# ---------------------------------------------------------------------------
# the fixture: `x` total, `y` absent at f=b, `w` a dense coefficient
# ---------------------------------------------------------------------------

DIMS = {'f': {'values': ['a', 'b']}, 't': {'dtype': 'int', 'values': [0, 1]}}

DATA = {
    'gate': pd.Series({'a': True}),
    'w': pd.Series({'a': 2.0, 'b': 3.0}),
}


def _model(equations: str | list[dict], *, objective: str = 'sum(x, over=f)', foreach: list[str] | None = None) -> dict:
    """A model whose only variable content is *equations*, in a binding row."""
    return {
        'dimensions': dict(DIMS),
        'parameters': {'gate': {'dims': ['f'], 'dtype': 'bool'}, 'w': {'dims': ['f']}},
        'variables': {
            'x': {'foreach': ['f', 't'], 'bounds': {'lower': 0, 'upper': 100}},
            'y': {'foreach': ['f', 't'], 'where': 'gate', 'bounds': {'lower': 0, 'upper': 50}},
        },
        'constraints': {
            'c': {
                'foreach': foreach if foreach is not None else ['t'],
                'equations': [{'expression': equations}] if isinstance(equations, str) else equations,
            }
        },
        'objectives': {'o': {'sense': 'maximize', 'equations': [{'expression': objective}]}},
    }


def _objective_of(
    equations: str | list[dict],
    objective: str = 'sum(x, over=f)',
    foreach: list[str] | None = None,
) -> float:
    """Solve *equations* on both lanes and the LP file; return the agreed value.

    ``differential`` raises if the three disagree, so a number coming back out
    of here is already a statement that the lanes concur about this spelling.
    """
    with differential(_model(equations, objective=objective, foreach=foreach), DATA, lp=True) as run:
        return float(run.result.objective)


# ---------------------------------------------------------------------------
# laws — these must hold
# ---------------------------------------------------------------------------

LAWS = [
    pytest.param(
        'sum(x + w * x, over=f) <= 120',
        'sum(w * x + x, over=f) <= 120',
        id='commutative-add',
    ),
    pytest.param(
        'sum((x + w * x) + x, over=f) <= 120',
        'sum(x + (w * x + x), over=f) <= 120',
        id='associative-add',
    ),
    pytest.param(
        'sum(x - w * x, over=f) <= 120',
        'sum(x + (-1) * w * x, over=f) <= 120',
        id='subtraction-is-negated-addition',
    ),
    pytest.param(
        'sum(w * (x + x), over=f) <= 120',
        'sum(w * x + w * x, over=f) <= 120',
        id='distributive-over-a-variable-free-factor',
    ),
    pytest.param(
        'sum((x + x) / w, over=f) <= 120',
        'sum(x / w + x / w, over=f) <= 120',
        id='distributive-over-a-divisor',
    ),
    pytest.param(
        # linearity of the reduction, which holds while nothing is absent
        'sum(x + w * x, over=f) <= 120',
        'sum(x, over=f) + sum(w * x, over=f) <= 120',
        id='reduction-is-linear-when-every-operand-is-total',
    ),
    pytest.param(
        'sum(roll(roll(x, t=1), t=-1), over=f) <= 120',
        'sum(x, over=f) <= 120',
        id='roll-is-invertible',
    ),
    pytest.param(
        # absence is *allowed* here: both spellings carry the same absence, so
        # the law survives it. This is what makes the non-laws below meaningful
        # rather than "anything with a mask behaves oddly".
        'sum(y + w * y, over=f) <= 120',
        'sum(w * y + y, over=f) <= 120',
        id='commutative-add-under-absence',
    ),
]


@pytest.mark.parametrize(('left', 'right'), LAWS)
def test_the_two_spellings_build_the_same_model(left, right):
    assert _objective_of(left) == pytest.approx(_objective_of(right), rel=RTOL)


# ---------------------------------------------------------------------------
# non-laws — equal in ordinary algebra, deliberately unequal here
# ---------------------------------------------------------------------------


def test_a_reduction_does_not_distribute_over_addition_when_an_operand_is_absent():
    """The defect behind #311, pinned as the semantics it turned out to be.

    ``y`` is absent at ``f=b``, so the summand ``x + y`` is absent there too
    (absence propagates, SPEC §6) and the reduction skips that slot — taking the
    perfectly present ``x[b]`` with it. Summing each operand separately keeps
    it, because each is reduced over its own domain.

    Both are correct answers to *different questions*: "the total of the net,
    where the net is defined" against "the total in, minus the total out". The
    language declines to guess which was meant, which is the whole content of
    the v1 convention — distributing one into the other would read the absent
    ``y`` as a zero.

    The relational lane used to distribute, so it answered the second question
    for both spellings and disagreed with linopy by 40% with no error.
    """
    together = _objective_of('sum(x + y, over=f) <= 120')
    apart = _objective_of('sum(x, over=f) + sum(y, over=f) <= 120')

    # `together` binds only at f=a, so x[b] is free to its own bound
    assert together == pytest.approx(400.0, rel=RTOL)
    # `apart` keeps x[b] in the row, so the cap actually binds the total
    assert apart == pytest.approx(240.0, rel=RTOL)
    assert together != pytest.approx(apart, rel=RTOL), 'the two questions must stay distinguishable'


def test_a_term_whose_variable_is_absent_is_not_a_term_worth_zero():
    """Row absence, the other half of the same rule.

    ``x + y >= k`` is *no constraint* where ``y`` is absent — not ``x >= k``.
    Compared against the spelling SPEC §6 points at for the other reading (two
    equations under complementary ``where`` clauses), so the test states the
    *difference between the two intents* rather than the behaviour alone.
    """
    minimise_x = '(-1) * x'
    propagated = _objective_of('x + y >= 60', objective=minimise_x, foreach=['f', 't'])
    zero_filled = _objective_of(
        [
            {'expression': 'x + y >= 60', 'where': 'y'},
            {'expression': 'x >= 60', 'where': 'NOT y'},
        ],
        objective=minimise_x,
        foreach=['f', 't'],
    )

    # f=a: y covers 50 of the 60, so x is pushed to 10. f=b: no row at all,
    # so x is free to fall to 0 — the absence removed the requirement.
    assert propagated == pytest.approx(-(10.0 + 10.0), rel=RTOL)
    # asking for zero-fill explicitly puts the requirement back at f=b
    assert zero_filled == pytest.approx(-(10.0 + 10.0 + 60.0 + 60.0), rel=RTOL)


def test_shift_and_a_filled_shift_are_different_operators():
    """``fill=`` is not decoration: it decides whether the row exists at all.

    Bare, the vacated slot is absent and the row goes with it (#289). Filled, it
    contributes the identity of the position it sits in and the row survives.
    """
    bare = _objective_of('sum(x - shift(x, t=1), over=f) <= 10')
    filled = _objective_of('sum(x - shift(x, t=1, fill=0), over=f) <= 10')

    assert bare != pytest.approx(filled, rel=RTOL), (
        'a bare shift drops the first row; a filled one keeps it, so these cannot agree'
    )
