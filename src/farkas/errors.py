"""The exception hierarchy, so that one ``except`` clause covers the package.

Before this module the package raised four unrelated ``ValueError``
subclasses and a great deal of bare ``ValueError``, which left a caller no
way to say "this model is the problem" without also catching every
``ValueError`` pandas or pydantic might raise on the way past.

The split that matters is **the model versus the run**:

* :class:`LanguageError` — the file says something the language does not
  accept. Nothing about the data would change the outcome; it is decidable at
  load time, and ``fk.check()`` raises exactly these.
* :class:`DataError` — the file is fine; what was bound to it is not. An
  unbound source, a column that does not carry the declared dims.

Everything subclasses :class:`LinopyYamlError`, which subclasses
``ValueError`` — so code that catches ``ValueError`` today keeps working.

One gap, on purpose: ``schema.py``'s field validators keep raising plain
``ValueError``, because pydantic collects those into its own
``pydantic.ValidationError`` (itself a ``ValueError``) and a custom class
would not survive the trip.

Deliberately dependency-free: the relational engine imports this module and
nothing else from the package (docs/ARCHITECTURE.md, hard rule 2).
"""

from __future__ import annotations

import difflib
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


class LinopyYamlError(ValueError):
    """Base class for every error this package raises on purpose."""


# ---------------------------------------------------------------------------
# The model is the problem — decidable without data
# ---------------------------------------------------------------------------


class LanguageError(LinopyYamlError):
    """The model is not sayable in the language, or does not obey its rules."""


class SchemaError(LanguageError):
    """A declaration is malformed: unknown key, bad dtype, colliding name."""


class DimensionError(LanguageError):
    """A dim-set rule was violated. Raised at load time, before any data."""


class PiecewiseExpansionError(LanguageError):
    """A piecewise block references something that doesn't exist or collides."""


# ---------------------------------------------------------------------------
# The run is the problem — the model was fine
# ---------------------------------------------------------------------------


class DataError(LinopyYamlError):
    """Data bound to a valid model is missing or the wrong shape."""


class NoSolutionError(LinopyYamlError):
    """The solve returned no values to read — infeasible, unbounded, errored.

    Neither the model nor the data was wrong; the answer is that there is no
    answer. It has its own class because the caller's response differs: a
    scenario sweep catches this and records the outcome, where a
    :class:`LanguageError` means the file needs editing.
    """


__all__ = [
    'DataError',
    'DimensionError',
    'LanguageError',
    'LinopyYamlError',
    'NoSolutionError',
    'PiecewiseExpansionError',
    'SchemaError',
]


def sparse_divisor_message(name: str, missing: int) -> str:
    """Why a divisor may not be sparse — one wording, both lanes.

    Everywhere else a missing parameter row is a zero coefficient (SPEC §6), and
    a zeroed term is a term that does not participate: the row survives and
    still says something. In divisor position there is no fill that preserves
    the constraint — 0 divides by zero, 1 silently rescales, and dropping the
    term rewrites what the row asserts — so the language refuses rather than
    picking one. That is v1's own argument for not filling on a caller's behalf,
    at the one position where it has no identity to fall back on.
    """
    return (
        f"parameter '{name}' is used as a divisor but covers {missing} fewer "
        f'coordinates than it is indexed over. A missing row means a zero '
        f'coefficient everywhere else, and zero is not a divisor: the term '
        f'would drop and the constraint would silently stop constraining.\n'
        f'  Supply the missing rows, or mask the coordinates out with a where.'
    )


def null_bounds_message(name: str, rows: int) -> str:
    """A bound with no value — one wording, both lanes.

    A missing row is a zero coefficient in a product (SPEC §6) and nothing at
    all in a bound: unbounded is not bounded-at-zero, and guessing either way
    changes which solutions exist. So both lanes refuse, and both say so while
    the model is still being built rather than letting the gap reach a sink.

    Naming both exits is the point. They are not two spellings of one repair —
    supplying the value keeps the variable and bounds it, masking removes the
    variable from every row and from the solution. A message that named only one
    would be choosing the model on the caller's behalf, which is the thing the
    refusal exists to avoid.
    """
    return (
        f"variable '{name}': {rows} rows have NULL bounds — a bound parameter is missing "
        f'values for some coordinates. The two ways out build different models, so the '
        f'language will not pick one:\n'
        f'  supply the value           the variable exists there, bounded (`inf` is a value)\n'
        f'  where: "<the parameter>"   the variable does not exist there at all'
    )


def unknown_name_message(kind: str, name: str, known: Iterable[str]) -> str:
    """``unknown <kind> '<name>'``, plus the near miss or the declared set.

    The same shape as the loader's unknown-key error, deliberately: a reader who
    has met one has met both, and there were already two copies of this idiom in
    the tree before this one.

    Single-line on purpose. These are raised as ``KeyError``, whose ``str`` is
    the *repr* of its argument, so a newline arrives at the reader as a literal
    ``\\n``. The list is not truncated for the same reason the loader does not
    truncate: the answer is usually in it, and a caller reading a solution back
    by name has no other way to discover what the model actually built.
    """
    candidates = sorted(known)

    # A constraint block with several equations names them by position, so the
    # block's own name resolves to nothing and the caller is asking for a name
    # that was never built (#298). Nearest-match is actively unhelpful there: it
    # picks one sibling and implies the others do not exist. List them instead.
    positional = [c for c in candidates if re.fullmatch(rf'{re.escape(name)}_\d+', c)]
    if positional:
        return (
            f"unknown {kind} '{name}': it is a block of {len(positional)} equations, which are "
            f'named by position — {", ".join(positional)}.'
        )

    near = difflib.get_close_matches(name, candidates, n=1, cutoff=0.6)
    fix = f"Did you mean '{near[0]}'?" if near else f'Declared: {", ".join(candidates) or "nothing"}.'
    return f"unknown {kind} '{name}'. {fix}"
