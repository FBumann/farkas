"""Run the ladder: one subprocess per measurement, one JSON line per result.

    uv run python -m bench.run                       # the committed ladder
    uv run python -m bench.run --cases dispatch --sizes m
    uv run python -m bench.run --skip-gate           # timings only, when iterating

The parent process deliberately imports neither farkas nor linopy: it must not
warm an allocator or a module cache that a measurement would then inherit.

**The gate runs first.** Before anything is timed, the smallest rung of each
case is solved on both arms and the objectives compared. A perf number that
describes two different models is worse than no number, and the differential
test suite proves parity for the *language*, not for the data this harness
happens to generate.

Results go to a JSONL file whose first line is the machine fingerprint — which
is what stops the last harness's failure mode, numbers outliving any record of
what produced them.

**The run replaces that file, it does not add to it.** So a `--cases` or
`--sizes` narrower than the tables you are about to publish will leave those
tables with no provenance, silently — which is a worse failure than the one the
fingerprint prevents, because the file still looks complete. Publishing means
one invocation covering every rung the tables show:

    uv run python -m bench.run --sizes xs s m l d100 d50 d25 d08

Replacing rather than appending is deliberate: accumulated records from two
different working trees are indistinguishable once written, and the report
would mix them into one row.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.cases import CASES

if TYPE_CHECKING:
    from collections.abc import Sequence

RESULTS = Path(__file__).resolve().parent / 'results'
GATE_RTOL = 1e-9
_EXCEPTION = re.compile(r'^[\w.]*(Error|Exception)\b')
TRACKED = ('farkas', 'linopy', 'duckdb', 'highspy', 'polars', 'pandas', 'numpy', 'xarray', 'pyarrow')


def fingerprint() -> dict[str, Any]:
    """Everything a number needs to still mean something in six months."""
    versions = {}
    for pkg in TRACKED:
        try:
            versions[pkg] = version(pkg)
        except PackageNotFoundError:
            versions[pkg] = None
    return {
        'record': 'run',
        'platform': platform.platform(),
        'machine': platform.machine(),
        'processor': platform.processor() or platform.machine(),
        'python': platform.python_version(),
        'versions': versions,
    }


def _child(args: list[str], timeout: float) -> dict[str, Any]:
    """Run one measurement and parse its single JSON line."""
    proc = subprocess.run(
        [sys.executable, '-m', 'bench._run_case', *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=Path(__file__).resolve().parent.parent,
        check=False,
    )
    if proc.returncode != 0:
        # a failure is a result: an OOM at a given budget is exactly the kind of
        # thing this harness exists to find, so keep the exception line rather
        # than whatever happens to be at the tail of the traceback
        lines = proc.stderr.strip().splitlines()
        raised = [ln for ln in lines if _EXCEPTION.match(ln)]
        return {
            'error': raised[-1] if raised else f'exit {proc.returncode}',
            'stderr': '\n'.join(lines[-25:]),
        }
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith('{')]
    if not lines:
        return {'error': 'no JSON on stdout', 'stderr': proc.stderr.strip()[-500:]}
    return json.loads(lines[-1])


def gate(case: str, timeout: float, arms: Sequence[str] = ('farkas', 'linopy')) -> dict[str, Any]:
    """Solve the smallest rung on every arm; objectives must agree.

    Gating *the arms being timed* rather than a fixed pair: an arm that is
    fast because it built a different model is the one result this harness
    must never publish, and a third arm would otherwise be exempt from the
    check the first two answer to.
    """
    size = CASES[case].ladder[0].label
    results = {arm: _child(['solve', '--case', case, '--size', size, '--arm', arm], timeout) for arm in arms}
    failed = {arm: r['error'] for arm, r in results.items() if 'error' in r}
    if failed:
        return {'record': 'gate', 'case': case, 'size': size, 'passed': False, 'reason': failed, 'detail': results}

    objectives = {arm: r['objective'] for arm, r in results.items()}
    lo, hi = min(objectives.values()), max(objectives.values())
    relative_gap = abs(hi - lo) / max(abs(lo), 1e-12)
    return {
        'record': 'gate',
        'case': case,
        'size': size,
        'passed': relative_gap <= GATE_RTOL,
        'objectives': objectives,
        'relative_gap': relative_gap,
    }


def timings(case: str, sizes: list[str], arms: list[str], opts: argparse.Namespace) -> list[dict[str, Any]]:
    """Every (size, arm) combination, each in its own process."""
    out = []
    for size in sizes:
        for arm in arms:
            for repeat in range(opts.repeat):
                args = ['time', '--case', case, '--size', size, '--arm', arm]
                if arm == 'linopy':
                    args += ['--io-api', opts.io_api]
                record = _child(args, opts.timeout)
                # stamped by the parent so a *failed* run is still fully
                # identified — a failure is a result here
                record |= {'record': 'timing', 'case': case, 'size': size, 'arm': arm, 'repeat': repeat}
                _echo(record)
                out.append(record)
    return out


def _echo(record: dict[str, Any]) -> None:
    writer = record.get('io_api') or ''
    head = f'  {record["case"]:<10} {record["size"]:<3} {record["arm"]:<7} {writer:<10}'
    if 'error' in record:
        print(f'{head} FAILED — {record["error"]}')
        return
    gb = record['peak_rss_bytes'] / 1e9
    phases = ' '.join(f'{k} {v:.2f}s' for k, v in record['phases'].items())
    live = f'{record["counts"]["columns"] / 1e6:.2f}M cols'
    print(f'{head} {record["wall_seconds"]:>7.2f}s  peak {gb:>5.2f} GB  {live:>11}  ({phases})')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--cases', nargs='+', default=sorted(CASES), choices=sorted(CASES))
    ap.add_argument(
        '--sizes', nargs='+', default=['xs', 's', 'm'], help="rung labels, or 'all' for every rung a case has"
    )
    ap.add_argument('--arms', nargs='+', default=['farkas', 'linopy'])
    ap.add_argument('--repeat', type=int, default=1)
    ap.add_argument('--io-api', default='lp-polars')
    ap.add_argument('--timeout', type=float, default=3600.0)
    ap.add_argument('--skip-gate', action='store_true', help='time without checking the arms agree')
    ap.add_argument('--out', type=Path, default=RESULTS / 'latest.jsonl')
    opts = ap.parse_args(argv)

    records: list[dict[str, Any]] = [fingerprint()]
    print(f'{records[0]["platform"]} · python {records[0]["python"]}')
    print('  ' + '  '.join(f'{k} {v}' for k, v in records[0]['versions'].items() if v))

    if not opts.skip_gate:
        print('\nparity gate')
        for case in opts.cases:
            result = gate(case, opts.timeout, opts.arms)
            records.append(result)
            if not result['passed']:
                print(f'  {case:<10} FAILED — {result.get("reason", result)}')
                _save(opts.out, records)
                print('\nAborted: the arms do not agree, so nothing here would be worth timing.')
                return 1
            objectives = ', '.join(f'{a} {o:.10g}' for a, o in result['objectives'].items())
            print(f'  {case:<10} ok ({objectives})')

    print('\ntimings')
    for case in opts.cases:
        rungs = [r.label for r in CASES[case].ladder]
        # a rung a case does not have is skipped rather than an error: the
        # density sweep only exists on masked cases, and `--sizes all` should
        # still mean "everything this case has"
        sizes = rungs if 'all' in opts.sizes else [s for s in opts.sizes if s in rungs]
        records += timings(case, sizes, opts.arms, opts)

    _save(opts.out, records)
    print(f'\n{len(records)} records -> {opts.out}')
    return 0


def _save(out: Path, records: list[dict[str, Any]]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(''.join(json.dumps(r) + '\n' for r in records))


if __name__ == '__main__':
    raise SystemExit(main())
