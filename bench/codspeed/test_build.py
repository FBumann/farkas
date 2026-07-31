"""Did *this commit* allocate more? — the regression question, asked unasked.

The third harness, and the one that costs nothing to ask. ``bench/run.py``
publishes how lpspec compares to linopy. ``bench/regressions/`` compares lpspec
to itself, but only when someone remembers to: a ``trigger:bench`` label, a base
checkout, two measured passes in one job. This module asks the same question
continuously, because CodSpeed keeps the baseline for every commit on ``main``
— so a pull request gets a comparison the workflow never had to build.

**The metric is heap allocation.** CodSpeed's ``memory`` instrument tracks
individual allocations, which makes it the same kind of number memray gives
``bench/regressions/`` and right here for the same reason: within one lane the
bias sits on both sides of a diff and cancels, where ``ru_maxrss`` moves with
whatever else the runner was doing. That argument is made in full in
``bench/regressions/test_build.py``; it is not restated here, any more than
the verbs are — both suites build through ``bench/workloads.py``, so neither
can drift into measuring a different thing under the same name.

Wall time is not measured at all. The ``walltime`` instrument wants CodSpeed's
bare-metal runners, and a free GitHub runner's clock is exactly the noise
``bench.yml`` already declines to gate on.

**The rung is chosen by the instrument, not by what is interesting.** Tracking
every allocation individually stops being cheap somewhere around 2M of them, so
these run at ``s`` — the bottom of the ladder ``bench/regressions/`` uses, on
the same models at the same shapes, so the two suites can be read against each
other. The cost of a small rung is sensitivity, not noise: the fixed ~30 MiB is
a larger share of the number, which *damps* a real regression rather than
inventing one. Raise it if the job stays inside its budget.

**Nothing here gates.** The workflow is ``continue-on-error`` and no ruleset
names it; ``bench.yml`` remains the thing that fails a pull request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import pytest

from bench.cases import CASES
from bench.workloads import build_and_hand_over, build_and_write, split_sources

if TYPE_CHECKING:
    from collections.abc import Callable

#: (case, rung). Three cases because each stresses a different SQL shape
#: (bench/README.md): `dispatch` is raw throughput, `nodal` is sparsity as it
#: actually occurs, `transport` is the mapping-table joins. Not the ladder and
#: not every case — every row here is measured on every pull request.
WORKLOADS = [('dispatch', 's'), ('nodal', 's'), ('transport', 's')]


class Verb(Protocol):
    def __call__(self, case_name: str, sources: dict[str, str], coords: dict[str, str]) -> int: ...


def _measure(benchmark: Callable[..., int], verb: Verb, case_name: str, size: str) -> None:
    case = CASES[case_name]
    shape = case.shape(size)
    # Generated and split before the measured region. Writing the parquet is the
    # harness's cost, and a baseline that moved the first time a data file was
    # created would be measuring the cache.
    sources, coords = split_sources(case, case.data(shape))
    columns = benchmark(verb, case_name, sources, coords)
    # a benchmark that silently built the wrong model is worse than none
    assert 0 < columns <= shape.nominal_variables


@pytest.mark.parametrize(('case_name', 'size'), WORKLOADS, ids=lambda v: str(v))
def test_build_and_write(benchmark: Callable[..., int], case_name: str, size: str) -> None:
    _measure(benchmark, build_and_write, case_name, size)


@pytest.mark.parametrize(('case_name', 'size'), WORKLOADS, ids=lambda v: str(v))
def test_build_and_hand_over(benchmark: Callable[..., int], case_name: str, size: str) -> None:
    _measure(benchmark, build_and_hand_over, case_name, size)
