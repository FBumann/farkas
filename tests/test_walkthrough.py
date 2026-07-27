"""The architecture walkthrough must keep running, or it stops being true.

``examples/walkthrough.py`` calls the real pipeline stage by stage, so any
signature change in the modules it narrates breaks this test rather than
leaving a plausible-looking script that no longer matches the code.

Running is the weaker half. The script also *claims* things — that the macro is
gone by stage 3, that ``var_p`` has 18 rows and not 24, that the degree-1
ceiling is caught with no data bound. A script that merely executes proves none
of them: 18 could silently become 31 and CI would stay green. So its whole
output is committed as ``examples/walkthrough.out`` and compared line for line.
When the pipeline legitimately changes, regenerate it —

    uv run pytest tests/test_walkthrough.py --update-golden

— and the diff of that file is the review artifact: exactly how the
architecture's story changed, in the same PR that changed it.
"""

from __future__ import annotations

import contextlib
import difflib
import importlib.util
import io
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).parent.parent / 'examples'
WALKTHROUGH = EXAMPLES / 'walkthrough.py'
GOLDEN = EXAMPLES / 'walkthrough.out'


@pytest.fixture(scope='module')
def walkthrough():
    spec = importlib.util.spec_from_file_location('walkthrough', WALKTHROUGH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules['walkthrough'] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules['walkthrough']


@pytest.fixture(scope='module')
def output(walkthrough) -> str:
    """One run of the whole pipeline, shared by both tests.

    ``StringIO`` is not a tty, so the banners come out unstyled — the same
    plain text a shell redirect into the golden file would produce.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        walkthrough.main()
    return buffer.getvalue()


def test_walkthrough_matches_golden(output: str, pytestconfig: pytest.Config) -> None:
    if pytestconfig.getoption('--update-golden'):
        GOLDEN.write_text(output)
        pytest.skip(f'rewrote {GOLDEN.name} from this run')

    expected = GOLDEN.read_text()
    if output == expected:
        return

    diff = '\n'.join(
        difflib.unified_diff(
            expected.splitlines(),
            output.splitlines(),
            'walkthrough.out (committed)',
            'walkthrough.py (this run)',
            lineterm='',
        )
    )
    pytest.fail(
        'the walkthrough narrates something the pipeline no longer does.\n'
        'If this run is the correct story, regenerate the golden file:\n'
        '    uv run pytest tests/test_walkthrough.py --update-golden\n\n' + diff,
        pytrace=False,
    )


def test_walkthrough_claims_hold(output: str) -> None:
    """The golden file catches *any* change; this names the ones that matter.

    Redundant with the diff above by construction, and kept anyway: when one of
    these breaks, the failure says which architectural property lapsed instead
    of pointing at a line number.
    """
    for stage in range(1, 8):
        assert f'[{stage}]' in output, f'stage {stage} did not run'

    assert 'weighted_sum' in output and "FunctionCallNode(name='sum'" in output  # macro expanded away
    assert 'row absence' in output and 'not 24' in output  # a mask removes rows, not values
    assert 'ok (optimal)' in output
    assert 'degree 2' in output  # the degree-1 ceiling still bites
    assert 'caught by check()' in output  # ...and without data bound, so CI can run it
