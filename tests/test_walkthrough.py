"""The architecture walkthrough must keep running, or it stops being true.

``examples/walkthrough.py`` calls the real pipeline stage by stage, so any
signature change in the modules it narrates breaks this test rather than
leaving a plausible-looking script that no longer matches the code.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

WALKTHROUGH = Path(__file__).parent.parent / 'examples' / 'walkthrough.py'


@pytest.fixture(scope='module')
def walkthrough():
    spec = importlib.util.spec_from_file_location('walkthrough', WALKTHROUGH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules['walkthrough'] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules['walkthrough']


def test_walkthrough_runs_every_stage(walkthrough, capsys):
    walkthrough.main()
    out = capsys.readouterr().out

    for stage in range(1, 8):
        assert f'[{stage}]' in out, f'stage {stage} did not run'

    # the claims the output makes, checked rather than narrated
    assert 'weighted_sum' in out and "FuncCallNode(name='sum'" in out  # macro expanded away
    assert 'var_p' in out and 'not 24' in out  # where is row absence
    assert 'Optimal' in out
    assert 'degree 2' in out  # the degree-1 ceiling still bites
    assert 'caught by check()' in out  # ...and without data bound, so CI can run it
