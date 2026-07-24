"""Shared fixtures for linopy_yaml tests.

On a bare install (no [compat] extra) the compat/oracle modules skip
themselves: they reach the oracle through ``tests.oracle``, whose
``importorskip`` guard fires at collection. There is no list of filenames to
keep in sync here — a module that needs the extra says so by importing it.
"""

from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).parent.parent / 'examples'


@pytest.fixture
def dispatch_yaml() -> Path:
    return EXAMPLES_DIR / 'dispatch.yaml'
