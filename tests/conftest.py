"""Shared fixtures for linopy_yaml tests."""

import importlib.util
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).parent.parent / 'examples'

# On a bare install (no [oracle] extra) the compat/oracle test modules cannot
# even be collected — they import linopy/xarray at module level. Skip them
# wholesale; the native suite (api, engine, language, architecture) still runs.
if importlib.util.find_spec('linopy') is None:
    collect_ignore = [
        'test_accessor.py',
        'test_dispatch.py',
        'test_error_notes.py',
        'test_group_sum.py',
        'test_loader.py',
        'test_lowering.py',
        'test_milp.py',
        'test_parser.py',
        'test_piecewise_block.py',
        'test_piecewise_convex.py',
        'test_relational.py',
        'test_roll.py',
        'test_validation.py',
        'test_expansion.py',
    ]


@pytest.fixture
def dispatch_yaml() -> Path:
    return EXAMPLES_DIR / 'dispatch.yaml'


def pytest_configure(config):
    # The oracle/compat layer is optional at runtime; when linopy is
    # installed (dev/CI), apply the patches so differential tests can use
    # Model.from_yaml. Native-path tests must not rely on this — the
    # linopy-free guarantee is asserted in a subprocess (test_api.py).
    import contextlib

    with contextlib.suppress(ImportError):
        import linopy_yaml.compat  # noqa: F401
