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
