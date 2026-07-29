"""ruff is pinned twice. This is what keeps the two pins equal.

The version lives in ``pyproject.toml`` (``ruff==`` in the dev group, which is
what CI installs and runs) and again in ``.pre-commit-config.yaml`` (the
``ruff-pre-commit`` rev, which is what the hook installs into its own isolated
environment). Nothing makes them agree on its own.

Dependabot manages both, but in *separate* PRs — it groups within an ecosystem
and never across one, so a ruff release produces one PR against the dev group
and another against the hook rev. Merge either alone and the formatter that
runs on commit is a different version from the one that gates the branch. That
shows up as a commit that was clean locally and fails CI, or the reverse, and
the cause is two files nobody thought to read together.

So: fail here instead, on the merge that introduced the skew, naming both
files. The fix is always to land the other PR.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: The `rev:` on the ruff-pre-commit repo block, e.g. `v0.16.0`.
_HOOK_REV = re.compile(r'ruff-pre-commit\s*\n\s*rev:\s*(\S+)')


def _pinned_in_pyproject() -> str:
    groups = tomllib.loads((REPO / 'pyproject.toml').read_text())['dependency-groups']
    pins = [spec for spec in groups['dev'] if spec.startswith('ruff==')]
    assert len(pins) == 1, f'expected exactly one `ruff==` pin in the dev group, found {pins}'
    return pins[0].removeprefix('ruff==')


def _pinned_in_pre_commit() -> str:
    match = _HOOK_REV.search((REPO / '.pre-commit-config.yaml').read_text())
    assert match is not None, 'no `rev:` found for the ruff-pre-commit repo'
    # the hook tags releases as `vX.Y.Z`; the package is `X.Y.Z`
    return match[1].removeprefix('v')


def test_ruff_is_the_same_version_in_ci_and_in_the_hook():
    pyproject, pre_commit = _pinned_in_pyproject(), _pinned_in_pre_commit()
    assert pyproject == pre_commit, (
        f'ruff is {pyproject} in pyproject.toml but {pre_commit} in .pre-commit-config.yaml — '
        f'the hook and CI would disagree about formatting. Dependabot bumps these in two '
        f'separate PRs; land the other one, or match them by hand.'
    )
