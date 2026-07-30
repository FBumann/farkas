"""One case, one arm, one process — the only place a measurement is taken.

Every run is its own interpreter. That is not tidiness: peak RSS is a property
of a *process*, and a second arm in the same one would inherit the first's
high-water mark and its warm allocator. The parent (``bench/run.py``) never
imports lpspec or linopy for this reason.

Output is exactly one JSON object on stdout. Anything else the libraries print
goes to stderr and is the parent's problem.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.cases import CASES

if TYPE_CHECKING:
    from bench.cases import Case, Shape


def peak_rss_bytes() -> int:
    """The kernel's own high-water mark — no tracker, no slowdown.

    ``ru_maxrss`` is bytes on macOS and kilobytes on Linux; this is the same
    counter ``/usr/bin/time -l`` reports, which is why the two agree.
    """
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == 'darwin' else peak * 1024


class Phases:
    """Wall time per named phase, so a total never hides where it went."""

    def __init__(self) -> None:
        self.times: dict[str, float] = {}
        self._start = time.perf_counter()

    def mark(self, name: str) -> None:
        now = time.perf_counter()
        self.times[name] = now - self._start
        self._start = now

    def reset(self) -> None:
        """Restart the clock, so untimed work between phases is not charged."""
        self._start = time.perf_counter()


def _run_lpspec(
    case: Case, paths: dict[str, str], lp: Path, phases: Phases, opts: argparse.Namespace
) -> dict[str, Any]:
    import lpspec as lps
    from lpspec.relational.sinks.highs import build_highs

    # The parameter/dimension split is harness bookkeeping — it re-parses the
    # YAML only because the runner, not lpspec, decides which parquet file is
    # which. Doing it before the clock starts is the difference between timing
    # the engine and timing the harness; the linopy arm has no counterpart.
    sources, coords = _split_sources(case, paths)

    phases.mark('import')
    ex = lps.build(case.model, sources, coords=coords)
    phases.mark('build')
    if opts.sink == 'lp':
        ex.write_lp(lp)
    else:
        # the hand-off, and nothing past it. `run()` is never called: the
        # simplex is the same work whoever filled the model, so timing it would
        # swamp the phase this harness exists to measure and publish a number
        # about HiGHS under our name. `build_highs` is the seam that stops
        # there — linopy's `to_highspy()` is the same seam on the other side.
        _handle = build_highs(ex._tables())
    phases.mark('emit')

    # Read after the clock stops: counts are the harness's, not the engine's.
    # `matrix` is this engine's frame and an older one exposes its own shape,
    # so the nonzero count is optional — this runner is also driven against a
    # foreign checkout's `lpspec`, where only the two totals are common.
    tables = ex._tables()
    matrix = getattr(tables, 'matrix', None)
    counts = {
        'columns': tables.column_count,
        'rows': tables.row_count,
        'nonzeros': getattr(matrix, 'height', None),
    }

    phases.reset()
    ex.close()
    phases.mark('teardown')
    return {'phases': phases.times, 'counts': counts}


def _run_linopy(
    case: Case, paths: dict[str, str], lp: Path, phases: Phases, opts: argparse.Namespace
) -> dict[str, Any]:
    from lpspec import linopy as lpspec_linopy

    phases.mark('import')
    data, coords = case.eager_inputs(paths)
    m = lpspec_linopy.build(case.model, data=data, coords=coords)
    phases.mark('build')
    if opts.sink == 'lp':
        # progress defaults to `m._xCounter > 10_000`, so every rung above `xs`
        # would render tqdm bars the lpspec arm has no equivalent of — ~7% of
        # the write at 10M variables, and stderr noise in a harness that parses
        # stdout
        m.to_file(lp, io_api=opts.io_api, progress=False)
    else:
        # the same seam as the lpspec arm: both end holding a populated
        # `highspy.Highs` with `run()` never called
        _handle = m.to_highspy()
    phases.mark('emit')
    counts = {'columns': int(m.nvars), 'rows': int(m.ncons), 'nonzeros': None}
    return {'phases': phases.times, 'counts': counts}


ARMS = {'lpspec': _run_lpspec, 'linopy': _run_linopy}


def _builder(case: Case, paths: dict[str, str], arm: str):
    """Just the build, callable repeatedly — no sink, no teardown timing."""
    if arm == 'lpspec':
        import lpspec as lps

        sources, coords = _split_sources(case, paths)

        def build() -> None:
            lps.build(case.model, sources, coords=coords).close()
    else:
        from lpspec import linopy as lpspec_linopy

        def build() -> None:
            data, coords = case.eager_inputs(paths)
            lpspec_linopy.build(case.model, data=data, coords=coords)

    return build


def _run_loop(case: Case, paths: dict[str, str], opts: argparse.Namespace) -> dict[str, Any]:
    """The same model built repeatedly in one process.

    Two questions, two numbers. **First** is what a caller pays who builds one
    model and solves it — a fresh interpreter, and whatever lazy work each lane
    does on its first call lands here. **Steady** is what a rolling horizon
    pays for every model after the first. They differ by more than an order of
    magnitude on the eager lane, so a single figure would misreport one of the
    two use cases whichever it was.

    Deliberately build-only: a sink is measured by `time`, and repeating one
    would conflate warm-up in the writer with warm-up in the engine.
    """
    build = _builder(case, paths, opts.arm)
    times = []
    for _ in range(opts.builds):
        start = time.perf_counter()
        build()
        times.append(time.perf_counter() - start)
    return {
        'first_build_seconds': times[0],
        'steady_build_seconds': min(times[1:]) if len(times) > 1 else None,
        'build_seconds': times,
    }


def _split_sources(case: Case, paths: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Parameters from dimension index tables, by what the model declares."""
    import yaml as pyyaml

    schema = pyyaml.safe_load(case.model.read_text())
    params = set(schema.get('parameters', {}))
    dims = set(schema.get('dimensions', {}))
    return (
        {k: v for k, v in paths.items() if k in params},
        {k: v for k, v in paths.items() if k in dims},
    )


def _objective(case: Case, shape: Shape, paths: dict[str, str], arm: str) -> float:
    """Solve, and return the objective the parity gate compares."""
    if arm == 'lpspec':
        import lpspec as lps

        sources, coords = _split_sources(case, paths)
        with lps.solve(case.model, sources, coords=coords) as sol:
            # two axes, not one: `status` is the coarse rollup ('ok') and the
            # solver's verdict is `termination_condition` ('optimal'). Testing
            # the wrong one aborted every run with a parity failure that was
            # really a vocabulary mismatch.
            if sol.termination_condition != 'optimal':
                raise RuntimeError(f'lpspec solve terminated {sol.termination_condition!r}, not optimal')
            return float(sol.objective)

    from lpspec import linopy as lpspec_linopy

    data, coords = case.eager_inputs(paths)
    m = lpspec_linopy.build(case.model, data=data, coords=coords)
    m.solve(solver_name='highs', output_flag=False)
    return float(m.objective.value)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('mode', choices=('time', 'solve', 'loop'))
    ap.add_argument(
        '--builds',
        type=int,
        default=5,
        help='loop mode: how many times to build the model in one process. The '
        'first is reported separately from the minimum of the rest.',
    )
    ap.add_argument('--case', required=True, choices=sorted(CASES))
    ap.add_argument('--size', required=True)
    ap.add_argument('--arm', required=True, choices=sorted(ARMS))
    ap.add_argument('--io-api', default='lp-polars', help="linopy arm's writer")
    ap.add_argument(
        '--sink',
        default='lp',
        choices=('lp', 'highs'),
        help='where the built model goes: an LP file, or straight into HiGHS',
    )
    ap.add_argument('--cache', type=Path, default=None)
    opts = ap.parse_args(argv)

    case = CASES[opts.case]
    shape = case.shape(opts.size)
    paths = case.data(shape) if opts.cache is None else case.data(shape, opts.cache)

    record: dict[str, Any] = {
        'case': case.name,
        'size': shape.label,
        'arm': opts.arm,
        'dimensions': shape.sizes,
        'nominal_variables': shape.nominal_variables,
        'density': shape.density,
        'io_api': opts.io_api if opts.arm == 'linopy' and opts.sink == 'lp' else None,
        'sink': opts.sink,
    }

    if opts.mode == 'loop':
        record |= _run_loop(case, paths, opts)
        record['peak_rss_bytes'] = peak_rss_bytes()
        print(json.dumps(record))
        return 0

    if opts.mode == 'solve':
        record['objective'] = _objective(case, shape, paths, opts.arm)
        record['peak_rss_bytes'] = peak_rss_bytes()
        print(json.dumps(record))
        return 0

    with tempfile.TemporaryDirectory(prefix='lpspec-bench-') as tmp:
        lp = Path(tmp) / f'{case.name}-{shape.label}.lp'
        # the clock starts before the arm's own imports, so a lane that pulls in
        # xarray on first use pays for it visibly instead of inside `build`
        result = ARMS[opts.arm](case, paths, lp, Phases(), opts)
        # the highs sink writes no artifact — that is the whole point of it
        record['lp_bytes'] = lp.stat().st_size if opts.sink == 'lp' else None

    record.update(result)
    # import is excluded — it is fixed, paid once per process, and at the
    # smallest rungs it is larger than the whole build. Every other phase
    # counts, teardown included: releasing a scratch database is work a user
    # pays for, and leaving it out would flatter the arm that has one.
    record['wall_seconds'] = sum(v for k, v in record['phases'].items() if k != 'import')
    record['live_fraction'] = record['counts']['columns'] / max(shape.nominal_variables, 1)
    record['peak_rss_bytes'] = peak_rss_bytes()
    record['threads'] = os.cpu_count()
    print(json.dumps(record))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
