"""Did *this change* make it worse? — one lane, tracked across versions.

The sibling of `bench/run.py`, asking the other question. `run.py` publishes
how lpspec compares to linopy and must therefore measure peak RSS, because RSS
is what a reader can check with `/usr/bin/time`. This module compares lpspec to
*itself* over time, where a different metric is not only allowed but better.

Why memray is right here and wrong there, measured on `dispatch/m`:

| arm | ru_maxrss | memray peak |
|---|---|---|
| lpspec | 309 MB | 211 MB |
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

**Whatever `lps.build` builds with.** Nothing here names an engine, so
``LPSPEC_ENGINE`` selects one and the same two commands answer the same
question for either — record a run, change something, compare the two run ids
by hand. A stored baseline does not record which engine produced it, so do not
compare across engines: both numbers are real, and the difference between them
is the engine rather than the change.
"""

from __future__ import annotations

from typing import Any

import pytest

from bench.cases import CASES, Shape
from bench.workloads import build_and_hand_over, build_and_write, split_sources

#: (case, rung). Small enough to run in a minute, large enough that fixed costs
#: do not dominate — a regression suite that only exercises the constant term
#: reports noise. The published ladder is where the big rungs live.
WORKLOADS = [('dispatch', 's'), ('dispatch', 'm'), ('nodal', 'm'), ('transport', 'm'), ('profiled', 'm')]


def _check(benchmark_memory: Any, columns: int, shape: Shape) -> None:
    """A benchmark that silently built the wrong model is worse than none.

    The published ladder has a parity gate against linopy; this has an
    arithmetic one, which is all a single-lane suite can afford.
    """
    assert columns > 0
    assert columns <= shape.nominal_variables
    benchmark_memory.extra_info['columns'] = columns
    benchmark_memory.extra_info['live_fraction'] = columns / shape.nominal_variables


@pytest.mark.benchmem(isolate=True)
@pytest.mark.parametrize(('case_name', 'size'), WORKLOADS, ids=lambda v: str(v))
def test_build_and_write(benchmark_memory, case_name: str, size: str) -> None:
    case = CASES[case_name]
    shape = case.shape(size)
    # the same split the published harness uses, imported rather than restated
    sources, coords = split_sources(case, case.data(shape))
    _check(benchmark_memory, benchmark_memory(build_and_write, case_name, sources, coords), shape)


@pytest.mark.benchmem(isolate=True)
@pytest.mark.parametrize(('case_name', 'size'), WORKLOADS, ids=lambda v: str(v))
def test_build_and_hand_over(benchmark_memory, case_name: str, size: str) -> None:
    """Kept a separate test rather than a parametrisation of the one above.

    ``--benchmark-memory-compare`` matches stored baselines by test id, so
    adding a ``[lp]``/``[highs]`` axis to the existing test would orphan every
    baseline already recorded against it. A new id costs nothing by comparison.
    """
    case = CASES[case_name]
    shape = case.shape(size)
    sources, coords = split_sources(case, case.data(shape))
    _check(benchmark_memory, benchmark_memory(build_and_hand_over, case_name, sources, coords), shape)
