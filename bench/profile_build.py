"""Attribute build wall time to the queries that spend it.

``bench/run.py`` says *how much* slower we are; this says *where*. It wraps
``LazyFrame.collect`` and tags every collection with the build step that issued
it, so the output is a ranked list of queries rather than a single number.

Collection is the right thing to wrap: a lazy frame costs nothing until
something asks for its rows, so every second of the build is inside one of
these calls.

    uv run python -m bench.profile_build dispatch l
    uv run python -m bench.profile_build transport m

The wrapper adds Python overhead per call, so **absolute times here are not
comparable to ``bench/run.py``** — there are only a few dozen collections, but
the process is otherwise unoptimised. Read the shares, not the seconds; to
quote a number, measure it with the harness.
"""

from __future__ import annotations

import argparse
import collections
import time
from pathlib import Path
from typing import Any

from bench import cases as bench_cases

STEPS = (
    '_create_param_frame',
    '_create_dim_frames',
    '_label_frame',
    '_build_variable',
    '_build_constraint',
    '_build_objective',
)


def _instrument(timings: dict[Any, list[float]], phase: dict[str, str]) -> None:
    """Tag each collection with the build step that issued it."""
    import polars as pl

    from lpspec.relational.executor import PolarsExecutor

    original_collect = pl.LazyFrame.collect

    def collect(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return original_collect(self, *args, **kwargs)
        finally:
            elapsed = time.perf_counter() - started
            # the plan, flattened, is what identifies a query here — the same
            # role the SQL text plays in a statement-level profiler
            key = (phase['now'], ' '.join(self.explain(optimized=False).split())[:88])
            entry = timings.setdefault(key, [0.0, 0])
            entry[0] += elapsed
            entry[1] += 1

    pl.LazyFrame.collect = collect

    for name in STEPS:
        original_step = getattr(PolarsExecutor, name)

        def wrap(step, label):
            def wrapper(self, *args, **kwargs):
                previous, phase['now'] = phase['now'], label
                try:
                    return step(self, *args, **kwargs)
                finally:
                    phase['now'] = previous

            return wrapper

        setattr(PolarsExecutor, name, wrap(original_step, name))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('case', choices=sorted(bench_cases.CASES))
    parser.add_argument('size', help='a rung of the case ladder, e.g. xs s m l')
    parser.add_argument('--top', type=int, default=12, help='queries to list')
    args = parser.parse_args()

    timings: dict[Any, list[float]] = {}
    phase = {'now': 'setup'}
    _instrument(timings, phase)

    import lpspec as lps

    case = bench_cases.CASES[args.case]
    sources = case.data(case.shape(args.size))

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        started = time.perf_counter()
        with lps.build(case.model, sources) as executor:
            build = time.perf_counter() - started
            phase['now'] = 'emit'
            started = time.perf_counter()
            executor.write_lp(Path(tmp) / 'model.lp')
            emit = time.perf_counter() - started

    print(f'\n{args.case}/{args.size}: build {build:.2f}s, emit {emit:.2f}s')
    print('(instrumented — read the shares, not the seconds)\n')

    by_step: dict[str, list[float]] = collections.defaultdict(lambda: [0.0, 0])
    for (step, _), (elapsed, calls) in timings.items():
        by_step[step][0] += elapsed
        by_step[step][1] += calls
    total = sum(v[0] for v in by_step.values()) or 1.0

    print(f'{"step":24} {"seconds":>8} {"share":>7} {"calls":>7}')
    for step, (elapsed, calls) in sorted(by_step.items(), key=lambda kv: -kv[1][0]):
        print(f'{step:24} {elapsed:8.2f} {100 * elapsed / total:6.0f}% {calls:7d}')

    print(f'\ntop {args.top} queries')
    ranked = sorted(timings.items(), key=lambda kv: -kv[1][0])[: args.top]
    for (step, sql), (elapsed, calls) in ranked:
        print(f'  {elapsed:6.2f}s {100 * elapsed / total:4.0f}%  n={calls:<4d} [{step}]\n      {sql}')


if __name__ == '__main__':
    main()
