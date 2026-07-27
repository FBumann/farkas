"""How every batched pass in the engine picks its chunk size.

One rule, in one place: a pass has a *budget* in elements and walks *units*
that each carry ``width`` of them, so it takes ``budget // width`` units at a
time. Three passes need this — label assignment over a leading dim, the
constraint text, the solver hand-off — and they differ only in what a unit is.

The width is the part that gets forgotten, and forgetting it does not look
like a bug. ``solver_direct`` chunked the constraint matrix by rows with no
width at all, which reads as bounded and is not: a row is nine entries in one
model and a hundred in another, so what the pass held tracked the model's
shape rather than the budget — the one thing hard rule 4 says peak must not
do. Requiring a width at every call site is the point of this module. A pass
whose unit really does cost one element says so, in one character, where a
reviewer can see it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


def ranges(total: int, budget: int, width: float) -> Iterator[tuple[int, int]]:
    """Half-open ``[lo, hi)`` ranges covering ``[0, total)``.

    Each holds about ``budget`` elements, given that one unit costs ``width``
    of them. A ``width`` below 1 is read as 1 — a unit cannot cost less than
    itself, and a fractional average (0.4 nonzeros per row, in a model that is
    mostly bounds) would otherwise ask for chunks wider than the budget.

    Empty input yields nothing rather than one empty range: a caller looping
    over ``ranges`` should do no work, not one pass over nothing.
    """
    per_chunk = max(1, int(budget // max(1.0, width)))
    for lo in range(0, total, per_chunk):
        yield lo, min(lo + per_chunk, total)
