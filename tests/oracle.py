"""Guarded access to the compat/oracle lane.

Importing this module skips the importing test module when the ``[compat]``
extra is absent — ``pytest.importorskip`` raises ``Skipped``, and pytest turns
that into a skipped module at collection time.

Import the oracle *through here* rather than importing linopy or xarray
directly, so the guard cannot be bypassed by import ordering: isort sorts a
bare ``import xarray`` above a first-party import, and it would then blow up
as a collection error before any guard ran.

This replaces a hand-maintained list of filenames in ``conftest.py``, which
had to be edited every time a test module was added and silently mis-skipped
when it was not.
"""

from __future__ import annotations

import pytest

_REASON = 'needs the [compat] extra (linopy, xarray)'

linopy = pytest.importorskip('linopy', reason=_REASON)
xr = pytest.importorskip('xarray', reason=_REASON)

from linopy_yaml import compat, loader  # noqa: E402  — must follow the guard above

__all__ = ['compat', 'linopy', 'loader', 'xr']
