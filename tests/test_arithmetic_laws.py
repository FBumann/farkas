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

from farkas.errors import DataError
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


def _model(
    expression: str,
    *,
    objective: str = 'sum(x, over=f)',
    foreach: list[str] | None = None,
    also: dict | None = None,
) -> dict:
    """A model whose only variable content is *expression*, in a binding row.

    *also* adds a second named constraint, for the cases that need two rules —
    which are now two blocks rather than two entries in a list (#298).
    """
    return {
        'dimensions': dict(DIMS),
        'parameters': {'gate': {'dims': ['f'], 'dtype': 'bool'}, 'w': {'dims': ['f']}},
        'variables': {
            'x': {'foreach': ['f', 't'], 'bounds': {'lower': 0, 'upper': 100}},
            'y': {'foreach': ['f', 't'], 'where': 'gate', 'bounds': {'lower': 0, 'upper': 50}},
        },
        'constraints': {
            'c': {'foreach': foreach if foreach is not None else ['t'], 'expression': expression},
            **(also or {}),
        },
        'objectives': {'o': {'sense': 'maximize', 'expression': objective}},
    }


def _objective_of(
    expression: str,
    objective: str = 'sum(x, over=f)',
    foreach: list[str] | None = None,
    also: dict | None = None,
) -> float:
    """Solve *expression* on both lanes and the LP file; return the agreed value.

    ``differential`` raises if the three disagree, so a number coming back out
    of here is already a statement that the lanes concur about this spelling.
    """
    with differential(_model(expression, objective=objective, foreach=foreach, also=also), DATA, lp=True) as run:
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
    Compared against the spelling SPEC §6 points at for the other reading — two
    constraints under complementary ``where`` clauses — so the test states the
    *difference between the two intents* rather than the behaviour alone.
    """
    minimise_x = '(-1) * x'
    propagated = _objective_of('x + y >= 60', objective=minimise_x, foreach=['f', 't'])
    zero_filled = _objective_of(
        'x + y >= 60',
        objective=minimise_x,
        foreach=['f', 't'],
        also={'c_unsized': {'foreach': ['f', 't'], 'where': 'NOT y', 'expression': 'x >= 60'}},
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


# ---------------------------------------------------------------------------
# the same rules under wider shapes
#
# The cases above all reduce over `f` with one mask on `f`, which is the
# smallest arrangement that shows the rule — and small enough that it could be
# passing for the wrong reason. These vary one thing at a time: the *reduction*
# (`group_sum` rather than `sum`), the *number* of masks, where the mask *sits*
# relative to the dim being reduced, and where the absence *comes from*.
#
# `group_sum` is the one that had to be here. #314 routed it through the same
# propagation as `sum` and nothing covered it, so its behaviour was a claim
# rather than a result.
# ---------------------------------------------------------------------------

WIDE_DATA = {
    # `y` absent at d; `v` absent at b *and* d, so the two masks do not nest
    'gate': pd.Series([True, True, True], index=pd.Index(['a', 'b', 'c'], name='f')),
    'gate2': pd.Series([True, True], index=pd.Index(['a', 'c'], name='f')),
    'w': pd.Series([2.0, 3.0, 4.0, 5.0], index=pd.Index(['a', 'b', 'c', 'd'], name='f')),
}

WIDE_COORDS = {
    'f': pd.DataFrame({'f': ['a', 'b', 'c', 'd'], 'grp': ['g0', 'g0', 'g1', 'g1']}),
    'g': pd.Index(['g0', 'g1'], name='g'),
    't': pd.Index([0, 1], name='t'),
}

PLAIN_COORDS = {'f': pd.Index(['a', 'b', 'c', 'd'], name='f'), 't': pd.Index([0, 1], name='t')}


def _wide_objective_of(expression: str, *, foreach: list[str]) -> float:
    # `g` and the coordinate that reaches it exist only for the grouped cases:
    # a dimension no declaration uses has no coordinate set to check against,
    # and the executor refuses it rather than carrying a dangling target.
    grouped = 'g' in foreach
    dims = (
        {'g': {}, 'f': {'coords': {'grp': 'g'}}, 't': {'dtype': 'int'}} if grouped else {'f': {}, 't': {'dtype': 'int'}}
    )
    model = {
        'dimensions': dims,
        'parameters': {
            'gate': {'dims': ['f'], 'dtype': 'bool'},
            'gate2': {'dims': ['f'], 'dtype': 'bool'},
            'w': {'dims': ['f']},
        },
        'variables': {
            'x': {'foreach': ['f', 't'], 'bounds': {'lower': 0, 'upper': 100}},
            'y': {'foreach': ['f', 't'], 'where': 'gate', 'bounds': {'lower': 0, 'upper': 50}},
            'v': {'foreach': ['f', 't'], 'where': 'gate2', 'bounds': {'lower': 0, 'upper': 50}},
        },
        'constraints': {'c': {'foreach': foreach, 'expression': expression}},
        'objectives': {'o': {'sense': 'maximize', 'expression': 'x'}},
    }
    with differential(model, WIDE_DATA, WIDE_COORDS if grouped else PLAIN_COORDS, lp=True) as run:
        return float(run.result.objective)


def test_group_sum_does_not_distribute_over_addition_either():
    """`group_sum` is a reduction, so the non-law applies to it unchanged.

    #314 routed it through the same absence propagation as `sum` on the argument
    that a group *is* a sum. Nothing tested that, so this is the assertion the
    change was made on: the two spellings separate, and both lanes agree about
    where they land.
    """
    together = _wide_objective_of('group_sum(x + y, over=f, by=grp) <= 120', foreach=['g', 't'])
    apart = _wide_objective_of('group_sum(x, over=f, by=grp) + group_sum(y, over=f, by=grp) <= 120', foreach=['g', 't'])

    assert together == pytest.approx(640.0, rel=RTOL)
    assert apart == pytest.approx(480.0, rel=RTOL)
    assert together != pytest.approx(apart, rel=RTOL)


def test_two_masks_intersect_rather_than_applying_one_at_a_time():
    """Three operands, two different masks — the summand needs *all* of them.

    `y` is absent at `d`, `v` at `b` and `d`, so the summand exists only at
    `a` and `c`. Worth its own case because the implementation collects one
    restriction per fragment and applies every one to every fragment; a version
    that stopped at the first, or that composed them pairwise down the addition
    tree, would still pass every single-mask test above.
    """
    together = _wide_objective_of('sum(x + y + v, over=f) <= 120', foreach=['t'])
    apart = _wide_objective_of('sum(x, over=f) + sum(y, over=f) + sum(v, over=f) <= 120', foreach=['t'])

    assert together == pytest.approx(640.0, rel=RTOL)
    assert apart == pytest.approx(240.0, rel=RTOL)


def test_a_broadcast_coefficient_does_not_move_where_the_summand_exists():
    """A sparse *parameter* is not absence, so it must not restrict anything.

    `w * x` is a term whose coefficient varies over `f`; multiplying by it
    changes the arithmetic and nothing about presence (SPEC §6: absence is a
    property of variables). The separation here must therefore come from `y`
    alone, exactly as in the un-weighted case.
    """
    together = _wide_objective_of('sum(w * x + y, over=f) <= 120', foreach=['t'])
    apart = _wide_objective_of('sum(w * x, over=f) + sum(y, over=f) <= 120', foreach=['t'])

    assert together == pytest.approx(320.0, rel=RTOL)
    assert apart == pytest.approx(120.0, rel=RTOL)


def test_a_mask_on_a_dim_the_reduction_does_not_touch_still_propagates():
    """Absence does not have to live on the summed dim to reach the summand.

    Here the mask is on `t` and the reduction is over `f`: at `t=1` the whole
    summand is absent for *every* `f`, so the row sums nothing rather than
    summing the surviving operand. The restriction is keyed by the dims the
    presence actually names, which is what makes this work — an implementation
    keying it by the reduced dim would silently do nothing here.
    """
    model = {
        'dimensions': {'f': {}, 't': {'dtype': 'int'}},
        'parameters': {'tgate': {'dims': ['t'], 'dtype': 'bool'}},
        'variables': {
            'x': {'foreach': ['f', 't'], 'bounds': {'lower': 0, 'upper': 100}},
            'y': {'foreach': ['f', 't'], 'where': 'tgate', 'bounds': {'lower': 0, 'upper': 50}},
        },
        'constraints': {'c': {'foreach': ['t'], 'expression': 'sum(x + y, over=f) <= 120'}},
        'objectives': {'o': {'sense': 'maximize', 'expression': 'x'}},
    }
    data = {'tgate': pd.Series([True], index=pd.Index([0], name='t'))}
    coords = {'f': pd.Index(['a', 'b'], name='f'), 't': pd.Index([0, 1], name='t')}

    with differential(model, data, coords, lp=True) as run:
        # t=0 the row binds; t=1 the summand is absent everywhere, so both x are free
        assert float(run.result.objective) == pytest.approx(320.0, rel=RTOL)


def test_shift_created_absence_reaches_a_reduction_like_any_other():
    """The two absence sources have to agree, or `shift` is a second rule.

    A bare `shift` vacates its first coordinate into absence (#289/#291), and
    that absence must behave inside a reduction exactly like a mask's. If it did
    not, the language would have two kinds of "not here" and SPEC §6 would be
    describing only one of them.

    The shifted operand is a **separate** variable from the one the objective
    maximises, and that is what makes the case discriminating. Written as
    ``sum(x + shift(x, t=1), over=f)`` it is not: the ``t=1`` row bounds the
    same ``x[.,0]`` that a missing restriction would bound at ``t=0``, so it
    dominates and the objective reads 120 either way. Verified by disabling the
    propagation — that spelling still passed while five other cases failed.

    Here ``v`` appears only under the shift, so ``t=0`` is the only place the
    rule can show: propagated, the summand is absent there and ``x[.,0]`` is
    free to its bounds; without it the row survives as ``sum(x, over=f) <= 120``
    and caps them at 120, giving 240.
    """
    model = {
        'dimensions': {'f': {}, 't': {'dtype': 'int'}},
        'parameters': {},
        'variables': {
            'x': {'foreach': ['f', 't'], 'bounds': {'lower': 0, 'upper': 100}},
            'v': {'foreach': ['f', 't'], 'bounds': {'lower': 0, 'upper': 100}},
        },
        'constraints': {'c': {'foreach': ['t'], 'expression': 'sum(x + shift(v, t=1), over=f) <= 120'}},
        'objectives': {'o': {'sense': 'maximize', 'expression': 'x'}},
    }
    coords = {'f': pd.Index(['a', 'b'], name='f'), 't': pd.Index([0, 1], name='t')}

    with differential(model, {}, coords, lp=True) as run:
        # t=0: the shifted operand vacates, so the summand is absent and the row
        # sums nothing — both x[.,0] stay at 100. t=1: the row binds, and `v` is
        # free to fall to 0, leaving 120 for x[.,1].
        assert float(run.result.objective) == pytest.approx(320.0, rel=RTOL)


def test_a_sparse_divisor_is_refused_rather_than_read_as_zero():
    """The one position with no defensible fill (#312).

    Everywhere else a missing parameter row is a zero coefficient (SPEC §6), and
    a zeroed term still leaves a row that says something. A divisor has no such
    identity: 0 divides by zero, 1 silently rescales, and dropping the term
    rewrites what the constraint asserts. Both lanes used to take that last
    option and *agree* about it — `x / d <= 10` became vacuous at the uncovered
    coordinate and `x` ran to its bound, objective 120 where the constraint
    reads as 20.

    Agreement is why the differential harness could not catch it: this is the
    shape of defect that needs a test saying what the answer *should* be, not
    that the lanes concur.
    """
    model = {
        'dimensions': {'f': {'values': ['a', 'b']}},
        'parameters': {'d': {'dims': ['f']}},
        'variables': {'x': {'foreach': ['f'], 'bounds': {'lower': 0, 'upper': 100}}},
        'constraints': {'c': {'foreach': ['f'], 'expression': 'x / d <= 10'}},
        'objectives': {'o': {'sense': 'maximize', 'expression': 'sum(x, over=f)'}},
    }
    sparse = {'d': pd.Series([2.0], index=pd.Index(['a'], name='f'))}

    with pytest.raises(DataError, match='used as a divisor'), differential(model, sparse) as run:
        _ = run.result.objective

    # covered, the same model builds and the row binds on both lanes
    dense = {'d': pd.Series([2.0, 5.0], index=pd.Index(['a', 'b'], name='f'))}
    with differential(model, dense, lp=True) as run:
        assert float(run.result.objective) == pytest.approx(70.0, rel=RTOL)


def test_a_divisor_may_be_sparse_where_the_row_is_masked_out():
    """The check is keyed to the rows built, not the coordinate product.

    Supplying a divisor only where the constraint exists is the ordinary idiom,
    and the first cut of this check refused it — the gap sits at coordinates the
    model already decided not to build. Kept as its own case because a check
    that ignores the mask still passes every test above: it only ever refuses
    *more*, and nothing else here asks it to accept something.
    """
    model = {
        'dimensions': {'f': {'values': ['a', 'b']}},
        'parameters': {'d': {'dims': ['f']}, 'active': {'dims': ['f'], 'dtype': 'bool'}},
        'variables': {'x': {'foreach': ['f'], 'bounds': {'lower': 0, 'upper': 100}}},
        'constraints': {'c': {'foreach': ['f'], 'where': 'active', 'expression': 'x / d <= 10'}},
        'objectives': {'o': {'sense': 'maximize', 'expression': 'sum(x, over=f)'}},
    }
    data = {
        'd': pd.Series([2.0], index=pd.Index(['a'], name='f')),
        'active': pd.Series([True], index=pd.Index(['a'], name='f')),
    }
    with differential(model, data, lp=True) as run:
        # f=a: the row binds at x <= 20. f=b: masked out, so x runs to its bound
        assert float(run.result.objective) == pytest.approx(120.0, rel=RTOL)


@pytest.mark.parametrize(
    ('label', 'variables', 'constraint', 'objective', 'expected'),
    [
        pytest.param(
            'where on the row',
            {'x': {'foreach': ['f'], 'bounds': {'lower': 0, 'upper': 100}}},
            {'foreach': ['f'], 'where': 'd', 'expression': 'x / d <= 10'},
            'sum(x, over=f)',
            120.0,
            id='mask-the-row',
        ),
        pytest.param(
            'mask the variable',
            {'x': {'foreach': ['f'], 'where': 'd', 'bounds': {'lower': 0, 'upper': 100}}},
            {'foreach': ['f'], 'expression': 'x / d <= 10'},
            'sum(x, over=f)',
            20.0,
            id='mask-the-variable',
        ),
    ],
)
def test_a_sparse_divisor_has_an_escape(label, variables, constraint, objective, expected):
    """Sparse data is the ordinary case, so the refusal must be escapable.

    Both spellings say the same thing in different places — *this coordinate has
    no row* — and either is enough, because the check asks where the model
    actually divides rather than whether the divisor is dense. A refusal with no
    way out would be worse than the silent answer it replaced.
    """
    model = {
        'dimensions': {'f': {'values': ['a', 'b']}},
        'parameters': {'d': {'dims': ['f']}},
        'variables': variables,
        'constraints': {'c': constraint},
        'objectives': {'o': {'sense': 'maximize', 'expression': objective}},
    }
    data = {'d': pd.Series([2.0], index=pd.Index(['a'], name='f'))}
    with differential(model, data, lp=True) as run:
        assert float(run.result.objective) == pytest.approx(expected, rel=RTOL), label
