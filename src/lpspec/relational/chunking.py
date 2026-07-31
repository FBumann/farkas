"""How every batched pass in the engine picks its chunk size.

One rule, in one place: a pass has a *budget* in elements and walks *units*
that each carry ``width`` of them, so it takes ``budget // width`` units at a
time. The passes that need it — the constraint text and the solver hand-off,
both reaching it through :class:`~lpspec.relational.sinks.tables.ModelTables`
— differ only in what a unit is: a row costs its average nonzeros, a column
costs one.

The width is the part that gets forgotten, and forgetting it does not look
like a bug. ``solver_direct`` chunked the constraint matrix by rows with no
width at all, which reads as bounded and is not: a row is nine entries in one
model and a hundred in another, so what the pass held tracked the model's
shape rather than the budget — the one thing a *batched* pass exists to stop
doing. (The engine's own peak tracks the model, deliberately: there is no
configured ceiling any more — ROADMAP Track 5. This is the narrower promise
that a pass over a model holds a bounded slice of it, not the whole.)
Requiring a width at every call site is the point of this module. A pass
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
