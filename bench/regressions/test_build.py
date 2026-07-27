"""Did *this change* make it worse? — one lane, tracked across versions.

The sibling of `bench/run.py`, asking the other question. `run.py` publishes
how farkas compares to linopy and must therefore measure peak RSS, because RSS
is what a reader can check with `/usr/bin/time`. This module compares farkas to
*itself* over time, where a different metric is not only allowed but better.

Why memray is right here and wrong there, measured on `dispatch/m`:

| arm | ru_maxrss | memray peak |
|---|---|---|
| farkas | 309 MB | 211 MB |
| linopy | 604 MB | **2967 MB** |

memray counts polars' reserved arenas as allocated and does not count the
interpreter and mapped libraries at all, so the bias points in opposite
directions in the two lanes: the peak ratio is 0.51x by RSS and 0.07x by
memray. A cross-library claim built on that would be false the moment anyone
checked. Within one lane the bias is the same on both sides of a diff and
cancels, leaving a metric that is deterministic and attributable to a call
stack — which machine-load-sensitive RSS is not.

`isolate=True` runs every pass in a fresh process, which matters twice: an engine
would otherwise be measured with a warm buffer pool, and it is what makes the
whole-process ``rss`` available alongside the memray peak, so the two can be
watched for divergence.

Not collected by ``uv run pytest`` — ``testpaths`` is ``tests``. Run:

    uv sync --group bench
    uv run pytest bench/regressions --benchmark-memory

    # against a stored baseline, failing on a regression
    uv run pytest bench/regressions --benchmark-memory-compare=NNNN \\
        --benchmark-memory-compare-fail=mean:10%
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from bench._run_case import _split_sources
from bench.cases import CASES

#: (case, rung). Small enough to run in a minute, large enough that fixed costs
#: do not dominate — a regression suite that only exercises the constant term
#: reports noise. The published ladder is where the big rungs live.
WORKLOADS = [('dispatch', 's'), ('dispatch', 'm'), ('nodal', 'm'), ('transport', 'm')]


def build_and_write(case_name: str, size: str, sources: dict[str, str], coords: dict[str, str]) -> int:
    """Build the model and stream it to an LP file; return the column count.

    Top-level and picklable on purpose: ``isolate=True`` sends this to a fresh
    process per pass. Data paths are resolved by the caller so that generating
    the parquet — which is neither farkas's work nor stable across machines —
    stays outside the measurement.
    """
    import farkas as fk

    with (
        tempfile.TemporaryDirectory(prefix='farkas-bench-') as tmp,
        fk.build(CASES[case_name].model, sources, coords=coords) as ex,
    ):
        ex.write_lp(Path(tmp) / 'model.lp')
        return ex._tables().column_count


@pytest.mark.benchmem(isolate=True)
@pytest.mark.parametrize(('case_name', 'size'), WORKLOADS, ids=lambda v: str(v))
def test_build_and_write(benchmark_memory, case_name: str, size: str) -> None:
    case = CASES[case_name]
    shape = case.shape(size)
    # the same split the published harness uses, imported rather than restated
    sources, coords = _split_sources(case, case.data(shape))

    columns = benchmark_memory(build_and_write, case_name, size, sources, coords)

    # a benchmark that silently built the wrong model would be worse than none:
    # the published ladder has a parity gate, this has an arithmetic one
    assert columns > 0
    assert columns <= shape.nominal_variables
    benchmark_memory.extra_info['columns'] = columns
    benchmark_memory.extra_info['live_fraction'] = columns / shape.nominal_variables
