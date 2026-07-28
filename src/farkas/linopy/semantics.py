"""Where this lane answers linopy's v1 convention.

linopy keeps its own ``linopy/semantics.py`` for the same reason: one home for
the convention, so the evaluator stays about evaluating and a later change to
the convention is a single-file diff rather than a hunt.

**What the convention asks of us.** linopy's v1 arithmetic convention (its
``doc/design/convention.rst``) makes *absence* — a labelled slot the model does
not cover — a first-class state. §6 propagates it through every operator and §12
then drops the constraint row, where the legacy convention quietly filled each
absent slot with 0. Crucially, §7 refuses to fill on the caller's behalf: the
right fill is 0 for a sum, 1 for a product, or "leave this row out" entirely,
and that is intent the library cannot see.

**Why the answers live here and not in the loader.** They are *positional*.
The same missing row means three different things depending on where the name
is used, and only the evaluator knows which:

============================  ==========================  ====================
position                      a missing row means         resolved by
============================  ==========================  ====================
coefficient (``w * x``)       zero                        :func:`coefficient`
``bounds:``                   nothing — it is an error    left NaN; raises
``where`` operand             false                       left NaN; SPEC §6
============================  ==========================  ====================

A single fill in ``load_parameters`` would have to pick one and would be wrong
for the other two: an unbounded variable is not a variable bounded at zero, and
a mask that reads true everywhere is not a mask.

Both functions are correct under the legacy convention too — they resolve
absence to the value legacy would have reached implicitly — so neither is
conditional on ``linopy.options["semantics"]``. What changes under v1 is only
that staying silent stopped being an option.
"""

from __future__ import annotations

from typing import Any

__all__ = ['coefficient', 'present', 'vacated']


def coefficient(parameter: Any) -> Any:
    """A parameter in a coefficient position, its uncovered slots at zero.

    A tidy parameter table is a **compressed dense array**, not a record of
    absence: supplying rows only for the live ``(flow, effect)`` pairs says the
    coefficient is zero elsewhere. That is the language's own sparsity idiom
    (SPEC §8, "sparse data gives sparse variables"), and the relational lane
    has always read it that way — ``_build_constraint`` left-joins each constant
    fragment and its docstring says so: "a coordinate it has no row for
    contributes zero".

    ``load_parameters`` reindexes to the master coordinates, so an uncovered
    slot arrives here as NaN, and v1 §5 refuses a NaN in a user-supplied
    constant outright — from inside linopy a deliberate absence and a data error
    are indistinguishable, so it declines to guess. Answering here is not a
    guess: zero is what the encoding already meant.
    """
    return parameter.fillna(0.0)


def present(variable: Any) -> Any:
    """A masked variable as a term contributing **zero** where it is absent.

    Today's language rule: a term carrying a masked-out variable drops out while
    the row it sits in survives, so ``x + y >= 10`` is ``x >= 10`` wherever ``y``
    is masked. The relational lane gets that from row absence — the join finds no
    row, the term is not emitted, the constraint row still is.

    Filling at the **leaf** is what preserves it. Fill any later and §2 has
    already absorbed the live terms sharing the slot (``(x + y).fillna(0)``
    yields a bare ``0 >= 10`` with ``x``'s coefficient gone); fill here and ``x``
    keeps it.

    ``to_linexpr()`` first, because ``Variable.fillna`` means two different
    things across the versions we support — a label fill routed through
    ``.where()`` on the released line, an expression fill on the v1 branch. The
    expression method is the stable one, and the one we want.

    .. note::
       This is the interim answer, not the settled one. v1 §6/§12 would drop the
       row instead, and its goal 1 ("no silent wrong answers") indicts the fill:
       ``x - relmax * size <= 0`` with ``size`` masked silently becomes
       ``x <= 0``. Adopting that reading is a change to *both* lanes and to
       SPEC §6, so it is its own commit — and it deletes this function.
    """
    return variable.to_linexpr().fillna(0)


def vacated(expression: Any) -> Any:
    """A shifted expression with its vacated edge positions at **zero**.

    ``shift`` is acyclic: it moves values along a dimension and leaves the
    positions at the edge with nothing to move in. SPEC §7 fixes what those
    contribute — "vacated positions contribute **zero**" — which is also the
    ``fill_value=0`` the DataArray branch of the same helper passes, so the rule
    is one rule whatever the operand is.

    linopy's v1 convention counts ``.shift()`` among the operations that *create*
    absence (§4), so without this the vacated slots would propagate (§6) and drop
    the row (§12) — an acyclic storage balance would silently lose its first
    timestep instead of starting from an empty store.

    Whether SPEC §7 should keep saying zero is a live question: zero on a
    constraint's right-hand side *pins* rather than relaxes, so
    ``x <= shift(dt, t=1)`` forces ``x <= 0`` at the first position unless it is
    masked. Until that is decided, this keeps the documented rule true by
    construction rather than by the legacy convention's accident.
    """
    return expression.fillna(0)
