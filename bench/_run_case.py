"""One case, one arm, one process — the only place a measurement is taken.

Every run is its own interpreter. That is not tidiness: peak RSS is a property
of a *process*, and a second arm in the same one would inherit the first's
high-water mark and its warm allocator. The parent (``bench/run.py``) never
imports farkas or linopy for this reason.

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


def _run_farkas(
    case: Case, paths: dict[str, str], lp: Path, phases: Phases, opts: argparse.Namespace
) -> dict[str, Any]:
    import farkas as fk

    # The parameter/dimension split is harness bookkeeping — it re-parses the
    # YAML only because the runner, not farkas, decides which parquet file is
    # which. Doing it before the clock starts is the difference between timing
    # the engine and timing the harness; the linopy arm has no counterpart.
    sources, coords = _split_sources(case, paths)

    phases.mark('import')
    ex = fk.build(case.model, sources, coords=coords)
    phases.mark('build')
    ex.write_lp(lp)
    phases.mark('emit')

    # read after the clock stops: counts are the harness's, not the engine's
    tables = ex._tables()
    counts = {
        'columns': tables.column_count,
        'rows': tables.row_count,
        'nonzeros': tables.matrix.height,
    }

    phases.reset()
    ex.close()
    phases.mark('teardown')
    return {'phases': phases.times, 'counts': counts, 'workdir_bytes': 0}


def _run_linopy(
    case: Case, paths: dict[str, str], lp: Path, phases: Phases, opts: argparse.Namespace
) -> dict[str, Any]:
    from farkas import linopy as farkas_linopy

    phases.mark('import')
    data, coords = case.eager_inputs(paths)
    m = farkas_linopy.build(case.model, data=data, coords=coords)
    phases.mark('build')
    # progress defaults to `m._xCounter > 10_000`, so every rung above `xs`
    # would render tqdm bars the farkas arm has no equivalent of — ~7% of the
    # write at 10M variables, and stderr noise in a harness that parses stdout
    m.to_file(lp, io_api=opts.io_api, progress=False)
    phases.mark('emit')
    counts = {'columns': int(m.nvars), 'rows': int(m.ncons), 'nonzeros': None}
    return {'phases': phases.times, 'counts': counts, 'workdir_bytes': 0}


ARMS = {'farkas': _run_farkas, 'linopy': _run_linopy}


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
    if arm == 'farkas':
        import farkas as fk

        sources, coords = _split_sources(case, paths)
        with fk.solve(case.model, sources, coords=coords) as sol:
            # two axes, not one: `status` is the coarse rollup ('ok') and the
            # solver's verdict is `termination_condition` ('optimal'). Testing
            # the wrong one aborted every run with a parity failure that was
            # really a vocabulary mismatch.
            if sol.termination_condition != 'optimal':
                raise RuntimeError(f'farkas solve terminated {sol.termination_condition!r}, not optimal')
            return float(sol.objective)

    from farkas import linopy as farkas_linopy

    data, coords = case.eager_inputs(paths)
    m = farkas_linopy.build(case.model, data=data, coords=coords)
    m.solve(solver_name='highs', output_flag=False)
    return float(m.objective.value)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('mode', choices=('time', 'solve'))
    ap.add_argument('--case', required=True, choices=sorted(CASES))
    ap.add_argument('--size', required=True)
    ap.add_argument('--arm', required=True, choices=sorted(ARMS))
    ap.add_argument('--io-api', default='lp-polars', help="linopy arm's writer")
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
        'io_api': opts.io_api if opts.arm == 'linopy' else None,
    }

    if opts.mode == 'solve':
        record['objective'] = _objective(case, shape, paths, opts.arm)
        record['peak_rss_bytes'] = peak_rss_bytes()
        print(json.dumps(record))
        return 0

    with tempfile.TemporaryDirectory(prefix='farkas-bench-') as tmp:
        lp = Path(tmp) / f'{case.name}-{shape.label}.lp'
        # the clock starts before the arm's own imports, so a lane that pulls in
        # xarray on first use pays for it visibly instead of inside `build`
        result = ARMS[opts.arm](case, paths, lp, Phases(), opts)
        record['lp_bytes'] = lp.stat().st_size

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
