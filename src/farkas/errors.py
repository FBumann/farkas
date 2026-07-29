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
    """
    return f"variable '{name}': {rows} rows have NULL bounds — a bound parameter is missing values for some coordinates"
