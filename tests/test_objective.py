"""What an objective sums, when its terms do not carry the same dims.

The rule is one line of SPEC §2 — an objective is a scalar, so every dim it
names is summed — and the whole content of these tests is *which* dims belong
to *what*. A term is summed over the dims that term carries. A term is not
repeated because a sibling term carries a dim it does not.

That distinction is invisible while every term of an objective has the same
dims, which is every model the rest of the suite builds. It becomes an 8x
error the moment an objective spans a sparse ``(snapshot, node, tech)``
variable and a dense ``(snapshot, node, carrier)`` one — the shape a real cost
function has, and the shape that found #197.
"""

from __future__ import annotations

import pandas as pd
import pytest

from tests.differential import differential

#: Two variables pinned to 1 on disjoint dims, so the objective is arithmetic
#: with no optimisation left in it: whatever comes out is what was summed.
DISJOINT_MODEL = {
    'dimensions': {
        'i': {'dtype': 'int', 'values': [0, 1]},
        'j': {'dtype': 'int', 'values': [0, 1, 2]},
        'k': {'dtype': 'int', 'values': [0, 1]},
    },
    'parameters': {'a': {'dims': ['i']}, 'b': {'dims': ['j']}, 'c': {'dims': ['k']}},
    'variables': {
        'x': {'foreach': ['i'], 'bounds': {'lower': 1, 'upper': 1}},
        'y': {'foreach': ['j'], 'bounds': {'lower': 1, 'upper': 1}},
    },
    'constraints': {'floor': {'foreach': ['i'], 'equations': [{'expression': 'x >= 0'}]}},
    'objectives': {'o': {'sense': 'minimize', 'equations': [{'expression': 'x * a + y * b'}]}},
}


@pytest.fixture
def data():
    """``sum(x * a) == 2``, ``sum(y * b) == 30``, ``sum(c) == 200``.

    Distinct enough that a broadcast shows up as a different number rather than
    a coincidence: broadcasting the first two gives 66, not 32.
    """
    return {
        'a': pd.Series([1.0, 1.0], index=pd.Index([0, 1], name='i')),
        'b': pd.Series([10.0, 10.0, 10.0], index=pd.Index([0, 1, 2], name='j')),
        'c': pd.Series([100.0, 100.0], index=pd.Index([0, 1], name='k')),
    }


@pytest.mark.parametrize(
    ('expression', 'expected', 'broadcast_would_give'),
    [
        ('x * a + y * b', 32.0, 66.0),
        ('x * a - y * b', -28.0, -54.0),
        ('-(x * a + y * b)', -32.0, -66.0),
        # the sum has to survive an operator applied to the group, or the fix
        # only holds at the top of the expression
        ('(x * a + y * b) * c', 6400.0, 13200.0),
        ('(x * a + y * b) / 2', 16.0, 33.0),
        # already scalar per term: the case that always agreed, kept as the
        # control that says the fix changed nothing it should not have
        ('sum(x * a, over=i) + sum(y * b, over=j)', 32.0, 32.0),
    ],
)
def test_a_term_is_summed_over_its_own_dims(data, expression, expected, broadcast_would_give):
    model = {**DISJOINT_MODEL, 'objectives': {'o': {'sense': 'minimize', 'equations': [{'expression': expression}]}}}
    with differential(model, data) as run:
        # `differential` already asserts the two lanes agree; what it cannot
        # know is whether they agree on the right number, and before #197 they
        # disagreed in exactly this shape
        assert run.oracle == pytest.approx(expected)
        assert run.oracle != pytest.approx(broadcast_would_give) or expected == broadcast_would_give
