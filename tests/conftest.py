"""Shared fixtures for linopy_yaml tests."""

from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


@pytest.fixture
def dispatch_yaml() -> Path:
    return EXAMPLES_DIR / "dispatch.yaml"


def pytest_configure(config):
    # The oracle/compat layer is optional at runtime; when linopy is
    # installed (dev/CI), apply the patches so differential tests can use
    # Model.from_yaml. Native-path tests must not rely on this — the
    # linopy-free guarantee is asserted in a subprocess (test_api.py).
    try:
        import linopy_yaml.compat  # noqa: F401
    except ImportError:
        pass
