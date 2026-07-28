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
CACHE = Path(__file__).resolve().parent / '.cache'

#: Arms whose engine is not on this branch, and the checkout each needs. The
#: value is filled from `--duckdb-root`; an arm with no root is skipped rather
#: than silently dropped, because "we did not run it" and "it has no row" look
#: identical in a table.
FOREIGN_ARMS = {'duckdb': None}
GATE_RTOL = 1e-9
_EXCEPTION = re.compile(r'^[\w.]*(Error|Exception)\b')
TRACKED = ('farkas', 'linopy', 'duckdb', 'highspy', 'polars', 'pandas', 'numpy', 'xarray', 'pyarrow')


def _commit(root: Path) -> str | None:
    """The commit *root* is checked out at, dirty flag included.

    Recorded because the version strings below fingerprint *installed
    distributions*, and an editable install reports the version it was synced
    at rather than the tree that ran. For an arm that is a checkout rather than
    a release — `duckdb` is — the commit is the only identifier there is.
    """
    try:
        head = subprocess.run(
            ['git', '-C', str(root), 'rev-parse', '--short', 'HEAD'],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
        dirty = subprocess.run(
            ['git', '-C', str(root), 'status', '--porcelain'],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    return f'{head}-dirty' if dirty else head


def fingerprint() -> dict[str, Any]:
    """Everything a number needs to still mean something in six months."""
    here = Path(__file__).resolve().parent.parent
    commits = {'farkas': _commit(here)}
    for arm, root in FOREIGN_ARMS.items():
        if root is not None:
            commits[arm] = _commit(root)
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
        # which *tree* ran, per arm — see _commit
        'commits': commits,
    }


def _child(
    args: list[str],
    timeout: float,
    root: Path | None = None,
    interpreter: Path | None = None,
) -> dict[str, Any]:
    """Run one measurement and parse its single JSON line.

    *root* runs the measurement out of a **different checkout**, with that
    checkout's own interpreter — which is how the `duckdb` arm works: the
    engine it measures does not exist on this branch, so the only honest way
    to compare against it is to let it run its own code. Both are pointed at
    one parquet cache, so the model is the same bytes on every arm.
    """
    here = Path(__file__).resolve().parent.parent
    # *root* runs another checkout's code with its python. *interpreter* runs
    # **this** checkout's code with another's python — which is how an
    # engine-agnostic driver reaches a foreign engine without being copied there.
    where = root or here
    borrowed = interpreter or root
    python = str(borrowed / '.venv' / 'bin' / 'python') if borrowed else sys.executable
    proc = subprocess.run(
        [python, '-m', 'bench._run_case', *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=where,
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


def loops(case: str, sizes: list[str], arms: list[str], opts: argparse.Namespace) -> list[dict[str, Any]]:
    """Build-only, repeated in one process: first model against every later one.

    A separate pass from `timings` because it answers a different question and
    must not disturb that one — a repeated build raises the high-water mark
    that `timings` reports as peak.

    Build-only also means sink-free, so this runs once per (size, arm) rather
    than once per sink.
    """
    out = []
    for size in sizes:
        for arm in arms:
            # A foreign arm needs no backport. The loop driver is
            # engine-agnostic — it calls `fk.build` and nothing else — so it
            # runs from *this* tree under *that* checkout's interpreter, which
            # is where its `farkas` lives. Only the python changes.
            root = FOREIGN_ARMS.get(arm)
            record = _child(
                [
                    'loop',
                    '--case',
                    case,
                    '--size',
                    size,
                    '--arm',
                    'farkas' if root else arm,
                    '--cache',
                    str(CACHE),
                    '--builds',
                    str(opts.builds),
                ],
                opts.timeout,
                interpreter=root,
            )
            record |= {'record': 'loop', 'case': case, 'size': size, 'arm': arm}
            first = record.get('first_build_seconds')
            steady = record.get('steady_build_seconds')
            if first is not None and steady is not None:
                print(
                    f'  {case:<10} {size:<3} {arm:<8} first {first * 1000:7.1f} ms  '
                    f'steady {steady * 1000:6.1f} ms  warm-up {(first - steady) * 1000:7.1f} ms'
                )
            out.append(record)
    return out


def gate(case: str, timeout: float, arms: Sequence[str] = ('farkas', 'linopy')) -> dict[str, Any]:
    """Solve the smallest rung on every arm; objectives must agree.

    Gating *the arms being timed* rather than a fixed pair: an arm that is
    fast because it built a different model is the one result this harness
    must never publish, and a third arm would otherwise be exempt from the
    check the first two answer to.
    """
    size = CASES[case].ladder[0].label
    results = {}
    for arm in arms:
        root = FOREIGN_ARMS.get(arm)
        spawn_as = 'farkas' if root else arm
        results[arm] = _child(
            ['solve', '--case', case, '--size', size, '--arm', spawn_as, '--cache', str(CACHE)],
            timeout,
            interpreter=root,
        )
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
    """Every (size, sink, arm) combination, each in its own process."""
    out = []
    for size in sizes:
        for sink in opts.sinks:
            for arm in arms:
                for repeat in range(opts.repeat):
                    # A foreign arm runs **this** harness under *its*
                    # interpreter, so both arms time the same code and only the
                    # engine differs — and the ladder is ours, so a case or a
                    # rung added since that checkout is still covered. Running
                    # its own `bench/` instead would silently limit the arm to
                    # whatever ladder it happened to ship with.
                    root = FOREIGN_ARMS.get(arm)
                    spawn_as = 'farkas' if root else arm
                    # An engine that takes a budget is really several arms:
                    # unbounded is *the engine*, comparable with a lane that has
                    # no such knob; a size is the engine plus a promise, which is
                    # what that architecture was for. Both, or the number means
                    # whichever one the reader assumed.
                    budgets = opts.duckdb_limits if arm == 'duckdb' else ['none']
                    for budget in budgets:
                        args = [
                            'time',
                            '--case',
                            case,
                            '--size',
                            size,
                            '--arm',
                            spawn_as,
                            '--sink',
                            sink,
                            '--cache',
                            str(CACHE),
                        ]
                        if arm == 'duckdb':
                            # `-1` is duckdb's own unlimited, passed explicitly:
                            # omitting the flag falls through to that checkout's
                            # 1GB *default*, so the "unbounded" arm would
                            # silently be another budgeted one. Only this arm
                            # takes the option at all.
                            args += ['--memory-limit', '-1' if budget == 'none' else budget]
                        if arm == 'linopy' and sink == 'lp':
                            args += ['--io-api', opts.io_api]
                        record = _child(args, opts.timeout, interpreter=root)
                        # stamped by the parent so a *failed* run is still fully
                        # identified — a failure is a result here
                        record |= {
                            'record': 'timing',
                            'case': case,
                            'size': size,
                            'arm': arm if budget == 'none' else f'{arm}@{budget}',
                            'sink': sink,
                            'repeat': repeat,
                        }
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
    ap.add_argument(
        '--duckdb-limits',
        nargs='+',
        default=['none', '1GB'],
        help="budgets to run the duckdb arm at. `none` passes duckdb's own unlimited "
        '(-1), which is the only setting comparable with a lane that has no such knob; '
        'a size runs it as its architecture intends, spilling to stay under. Both are '
        'reported, because a single number would be whichever one the reader assumed.',
    )
    ap.add_argument(
        '--duckdb-root',
        type=Path,
        default=None,
        help='checkout of the duckdb engine (with its own synced .venv) to run the '
        '`duckdb` arm from. That engine is not on this branch, so the arm is '
        'skipped without it rather than quietly absent.',
    )
    ap.add_argument('--repeat', type=int, default=1)
    ap.add_argument(
        '--builds',
        type=int,
        default=5,
        help='how many times the loop pass rebuilds each model in one process. 0 skips the pass.',
    )
    ap.add_argument('--io-api', default='lp-polars')
    ap.add_argument(
        '--sinks',
        nargs='+',
        default=['lp', 'highs'],
        choices=('lp', 'highs'),
        help='where each built model goes. Both by default: the LP file is the '
        'artifact fewest callers want, and it is not the same comparison — '
        "HiGHS's own model is resident in both arms and narrows the gap.",
    )
    ap.add_argument('--timeout', type=float, default=3600.0)
    ap.add_argument('--skip-gate', action='store_true', help='time without checking the arms agree')
    ap.add_argument('--out', type=Path, default=RESULTS / 'latest.jsonl')
    opts = ap.parse_args(argv)

    if opts.duckdb_root:
        root = opts.duckdb_root.resolve()
        if not (root / '.venv' / 'bin' / 'python').exists():
            ap.error(f'--duckdb-root {root} has no synced .venv — run `uv sync` in that checkout')
        FOREIGN_ARMS['duckdb'] = root
    unrooted = [a for a in opts.arms if a in FOREIGN_ARMS and FOREIGN_ARMS[a] is None]
    if unrooted:
        ap.error(f'arm(s) {unrooted} need a checkout to run from; pass --duckdb-root')

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

    if opts.builds:
        print('\nmarginal cost per model (build only, one process)')
        for case in opts.cases:
            rungs = [r.label for r in CASES[case].ladder]
            sizes = rungs if 'all' in opts.sizes else [s for s in opts.sizes if s in rungs]
            records += loops(case, sizes, opts.arms, opts)

    _save(opts.out, records)
    print(f'\n{len(records)} records -> {opts.out}')
    return 0


def _save(out: Path, records: list[dict[str, Any]]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(''.join(json.dumps(r) + '\n' for r in records))


if __name__ == '__main__':
    raise SystemExit(main())
