"""Claims the distribution metadata makes about itself, checked against the tree.

Metadata is the one part of the package no test exercises by importing it, so a
claim can be true in ``pyproject.toml`` and false in the wheel. It happened: the
rename moved ``src/linopy_yaml/`` to ``src/farkas/`` while a ``py.typed`` added
on a branch off the old layout merged back into the old path, leaving the
marker beside the package rather than inside it. Both halves looked right in
isolation.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).parent.parent
PYPROJECT = tomllib.loads((REPO / 'pyproject.toml').read_text())


def _package_dir() -> Path:
    """The one directory the wheel ships, read off the build config."""
    (packages,) = PYPROJECT['tool']['hatch']['build']['targets']['wheel']['packages']
    return REPO / packages


def test_typed_classifier_matches_the_marker():
    """``Typing :: Typed`` is a promise PEP 561 keeps only if the marker ships.

    Declaring the classifier without ``py.typed`` inside the package is worse
    than declaring neither: the index says the types are there and every
    downstream type checker still treats the import as untyped.
    """
    declared = 'Typing :: Typed' in PYPROJECT['project']['classifiers']
    marker = _package_dir() / 'py.typed'
    assert declared == marker.is_file(), (
        f'classifier says typed={declared} but {marker.relative_to(REPO)} '
        f'exists={marker.is_file()} — the marker must live *inside* the package '
        f'directory the wheel ships, not beside it'
    )


def test_src_holds_exactly_the_shipped_package():
    """A second directory under ``src/`` is either an unshipped module or debris.

    The rename left ``src/linopy_yaml/`` behind holding one file, which the
    sdist then carried because its include list is ``/src``.
    """
    strays = sorted(p.name for p in (REPO / 'src').iterdir() if p.name != _package_dir().name)
    assert not strays, f'unexpected entries under src/: {strays} — the wheel ships only {_package_dir().name}'
