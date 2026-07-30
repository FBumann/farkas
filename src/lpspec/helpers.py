"""The closed set of built-in helpers and their call shapes.

The set is closed: there is no Python registry. Both lanes therefore accept
exactly the same language, which is what makes the differential tests a
meaningful oracle (docs/ARCHITECTURE.md, hard rule 3). Compositions of these
built-ins belong in ``macros:``; math the language cannot say belongs in a
declared ``escape:`` island (#38), not in a helper that reads like a
built-in on the page.

This module is the *language* side of a helper — its name and its signature,
and nothing else. The signature lives here because four passes need it
(resolution types the dimension arguments, validation name-checks macro
bodies, lowering and the eager builder consume the call), and a helper whose
arity is spelled out once per pass is a helper the passes can disagree about.
It is imported by the linopy-free lane, so it must stay dependency-free — it
knows nothing of the AST, only counts and keyword names. The eager
evaluations live with the eager backend (``builder.py``); the relational ones
are lowering cases and SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_T = TypeVar('_T')


@dataclass(frozen=True)
class Builtin:
    """The call shape of one built-in helper.

    Keyword arguments come in three kinds, and which kind a name is decides
    what resolution turns its value into. ``dimension_kwargs`` name a dimension
    in the *value* (``sum(x, over=generator)``); ``coordinate_kwargs`` name a
    coordinate carried by the sibling ``over=`` dimension
    (``group_sum(x, over=line, by=to)``), so they are only meaningful together;
    ``dimension_is_key`` marks the helpers that instead name the dimension in
    the keyword *key* (``roll(x, snapshot=1)``) and therefore take exactly one,
    whatever it is called. ``usage`` is the one wording every lane quotes back.

    ``value_kwargs`` are the exception to all of that: a fixed keyword whose
    value is a number and whose key never names a dimension. There is one,
    ``shift(..., fill=0)``, and it exists so a caller can ask for the vacated
    positions to be filled rather than absent (§7). ``refusals`` names the
    keywords a helper deliberately does *not* take, with the reason, so the
    error can say why rather than only that the shape is wrong.
    """

    positional: int
    usage: str
    dimension_kwargs: tuple[str, ...] = ()
    coordinate_kwargs: tuple[str, ...] = ()
    dimension_is_key: bool = False
    value_kwargs: tuple[str, ...] = ()
    refusals: tuple[tuple[str, str], ...] = ()

    @property
    def keywords(self) -> frozenset[str]:
        """Every keyword the call must carry, when they are named at all."""
        return frozenset(self.dimension_kwargs) | frozenset(self.coordinate_kwargs)


BUILTINS: dict[str, Builtin] = {
    'sum': Builtin(1, 'sum(<expr>, over=<dim>)', dimension_kwargs=('over',)),
    'group_sum': Builtin(
        1,
        'group_sum(<expr>, over=<dim>, by=<coord>)',
        dimension_kwargs=('over',),
        coordinate_kwargs=('by',),
    ),
    'roll': Builtin(
        1,
        'roll(<expr>, <dim>=<n>)',
        dimension_is_key=True,
        refusals=(('fill', 'roll is cyclic, so no position is ever vacated'),),
    ),
    'shift': Builtin(
        1,
        'shift(<expr>, <dim>=<n>[, fill=0])',
        dimension_is_key=True,
        value_kwargs=('fill',),
    ),
}

BUILTIN_NAMES = frozenset(BUILTINS)


def call_shape_error(name: str, positional: int, kwargs: Iterable[str]) -> str | None:
    """Why a call to *name* does not fit its signature; ``None`` if it fits.

    Arity is a language rule, so it is checked in resolution — the pass every
    consumer goes through — and the same wording is available to any lane that
    wants to state it again.
    """
    builtin = BUILTINS[name]
    keys = set(kwargs)
    for refused, reason in builtin.refusals:
        if refused in keys:
            return f'{name}() takes no {refused}= — {reason}. Expects {builtin.usage}'
    named = keys - set(builtin.value_kwargs)
    fits = positional == builtin.positional and (
        len(named) == 1 if builtin.dimension_is_key else named == builtin.keywords
    )
    return None if fits else f'{name}() expects {builtin.usage}'


def split_dimension_key(name: str, kwargs: Mapping[str, _T]) -> tuple[str, _T, dict[str, _T]]:
    """A ``dimension_is_key`` call's dimension, its offset, and its value kwargs.

    Three passes unpack these kwargs (dim algebra, lowering, and the eager
    builder), and each of them wants "the one that names a dimension" rather
    than "the only one" — which stopped being the same thing when ``fill=``
    arrived. Splitting here keeps the three from disagreeing about which
    keyword is which.
    """
    builtin = BUILTINS[name]
    values = {k: v for k, v in kwargs.items() if k in builtin.value_kwargs}
    ((dim, by),) = ((k, v) for k, v in kwargs.items() if k not in values)
    return dim, by, values


def unknown_helper_message(name: str) -> str:
    """The one wording for "that is not a helper", shared by both lanes."""
    return (
        f"Unknown helper function '{name}'.\n"
        f'Available: {sorted(BUILTIN_NAMES)}\n'
        f"Define '{name}' as a macro under 'macros:' if it composes built-ins; "
        f'if the math is not sayable in the language, use a declared escape.'
    )
