"""How much of a case's peak is the *emit*, and how much is the build.

`bench/run.py` publishes farkas against linopy; `bench/regressions/` tracks the
whole build across versions. Neither splits the LP path in two, and the split
turns out to decide where optimising is worth anything.

Two measurements per case, each in its own process: build the model and stop,
then build it and write the LP. The difference is what the sink adds to peak
RSS — which is *not* what the sink allocates, and that gap is the point. Peak
RSS is a high-water mark, and the emit runs after the build has released its
transients, so sink allocation that fits under a mark the build already set
never appears. Measured on this branch at the `l` rung:

    dispatch    build 1,716 MiB -> total 2,138 MiB   (emit adds 421, 20%)
    transport   build 2,670 MiB -> total 2,719 MiB   (emit adds  49,  2%)

So `transport` — the case with the highest LP-path peak and the widest gap to
linopy — is entirely build-bound, and no sink change can reach it. A
micro-benchmark of the sink on its own says the opposite, because it measures
the sink against nothing: one reports the constraint section's sort holding
~975 MiB, essentially all of which hides under the build's mark here.

Repeats and a median, because the build's own peak is the noisy term: its
spread across three runs of identical code reaches 150 MiB, which is wider than
most changes worth measuring.

    uv run python -m bench.emit_peak --cases dispatch transport --size l
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MARKER = '##EMIT##'
_RSS_UNIT = 1 if sys.platform == 'darwin' else 1024


def peak_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _RSS_UNIT / 1024**2


def _measure(case_name: str, size: str, emit: bool) -> dict[str, object]:
    """Build the case, and write it or not. The caller reads peak at exit."""
    import farkas as fk
    from bench._run_case import _split_sources
    from bench.cases import CASES

    case = CASES[case_name]
    sources, coords = _split_sources(case, case.data(case.shape(size)))
    with fk.build(case.model, sources, coords=coords) as ex:
        record: dict[str, object] = {'nonzeros': ex._tables().matrix.height}
        if emit:
            with tempfile.TemporaryDirectory(prefix='farkas-emit-') as tmp:
                out = Path(tmp) / 'model.lp'
                ex.write_lp(out)
                record['bytes'] = out.stat().st_size
        return record


def _run(case_name: str, size: str, emit: bool, repeats: int) -> tuple[list[float], list[float], dict | str]:
    """Peaks, walls, and either the last record or the error that ended it."""
    peaks: list[float] = []
    walls: list[float] = []
    note: dict | str = 'no output'
    for _ in range(repeats):
        started = time.perf_counter()
        proc = subprocess.run(
            # as a module, not a path: the child imports `bench.cases`, and a
            # file run directly puts only its own directory on sys.path
            [sys.executable, '-m', 'bench.emit_peak', '--child', case_name, size, json.dumps(emit)],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        walls.append(time.perf_counter() - started)
        record = next((json.loads(x[len(MARKER) :]) for x in proc.stdout.splitlines() if x.startswith(MARKER)), None)
        if record is None:
            tail = (proc.stderr or proc.stdout).strip().splitlines()
            return peaks, walls, (tail[-1][:80] if tail else 'no output')
        peaks.append(record['peak'])
        note = record
    return peaks, walls, note


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', nargs='+', default=['dispatch', 'transport'])
    parser.add_argument('--size', default='l')
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()

    print(f'# emit share of peak — {args.size} rung, median of {args.repeats}\n')
    print('| case | nonzeros | build (MiB) | build + write (MiB) | emit adds | share | write s |')
    print('|---|---:|---:|---:|---:|---:|---:|')
    for case_name in args.cases:
        build_peaks, build_walls, note = _run(case_name, args.size, False, args.repeats)
        if build_peaks:
            emit_peaks, emit_walls, note = _run(case_name, args.size, True, args.repeats)
        if not build_peaks or not emit_peaks:
            print(f'| {case_name} | **{note}** | | | | | |')
            continue
        build = statistics.median(build_peaks)
        total = statistics.median(emit_peaks)
        nonzeros = note['nonzeros'] if isinstance(note, dict) else 0
        print(
            f'| {case_name} | {nonzeros:,} | {build:,.0f} | {total:,.0f} | '
            f'{total - build:,.0f} | {(total - build) / total:.1%} | '
            f'{statistics.median(emit_walls) - statistics.median(build_walls):.1f} |'
        )
    return 0


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--child':
        result = _measure(sys.argv[2], sys.argv[3], json.loads(sys.argv[4]))
        result['peak'] = peak_mib()
        print(MARKER + json.dumps(result))
        raise SystemExit(0)
    raise SystemExit(main())
