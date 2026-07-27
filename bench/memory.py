"""Peak-RSS harness: does the configured budget actually bound the build?

`docs/benchmarks.md` asserts that build peak is a function of the configured
``memory_limit`` rather than of model size, and `ARCHITECTURE.md` hard rule 4
states it as an invariant. The harness those numbers came from was deleted, so
nothing checks either claim. This is the replacement.

It measures the thing the old table left out: the **floor**. ``memory_limit``
governs duckdb's buffer manager, while peak RSS is a *whole-process* number
that also counts the interpreter, the duckdb/pyarrow shared objects, duckdb's
untracked per-thread operator scratch, and pages the allocator has not returned
to the OS. A budget can therefore only be judged against ``peak - baseline``,
so the baseline is measured as its own phase instead of being assumed away.

Every measurement runs in a fresh subprocess and reports that process's own
``ru_maxrss`` — the quantity ``/usr/bin/time -l`` reports, which
`docs/benchmarks.md` finding 4 makes the gate metric for exactly the reasons
recorded there (never benchmark runtime under memray). Sources are parquet
paths written by a separate process, so no input array is ever resident in a
measured one, and an out-of-memory failure is recorded as a result rather than
crashing the sweep — a budget that cannot be met is the most interesting datum
the sweep produces.

Usage::

    uv run python bench/memory.py                          # default sweep
    uv run python bench/memory.py --snapshots 100000 400000
    uv run python bench/memory.py --threads 1 2 4
    uv run python bench/memory.py --sink lp --json out.json

``--data-dir`` caches the generated parquet between runs; without it the
inputs are regenerated and removed each time.
"""

from __future__ import annotations

import argparse
import json
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

#: Child processes emit exactly one line starting with this, so the parent
#: never has to parse around duckdb's own chatter on stdout.
MARKER = '##BENCH##'

#: ``ru_maxrss`` is KiB on Linux and bytes on macOS. The original numbers in
#: docs/benchmarks.md are macOS; getting this wrong is a silent 1024x.
_RSS_UNIT = 1 if sys.platform == 'darwin' else 1024

#: Fraction of units zeroed out so `where: p_max > 0` actually masks — the
#: benchmarked model was G=100 with 89 active.
_MASKED_FRACTION = 0.11


def peak_rss_bytes() -> int:
    """This process's peak resident set size, in bytes."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _RSS_UNIT


def active_generators(generators: int) -> int:
    return generators - round(generators * _MASKED_FRACTION)


def dispatch_schema(generators: int) -> dict[str, Any]:
    """``examples/dispatch.yaml`` widened to *generators* units.

    Built as a dict rather than YAML text because ``farkas.api`` accepts one
    directly, which keeps the measured process free of a parse step whose cost
    is not what we are measuring.
    """
    return {
        'dimensions': {
            'snapshot': {'dtype': 'int'},
            'generator': {'values': [f'g{i:04d}' for i in range(generators)]},
        },
        'parameters': {
            'p_max': {'dims': ['generator']},
            'load': {'dims': ['snapshot']},
            'cost': {'dims': ['generator']},
        },
        'variables': {
            'p': {
                'foreach': ['snapshot', 'generator'],
                'where': 'p_max > 0',
                'bounds': {'lower': 0, 'upper': 'p_max'},
            },
        },
        'constraints': {
            'power_balance': {
                'foreach': ['snapshot'],
                'equations': [{'expression': 'sum(p, over=generator) == load'}],
            },
        },
        'objectives': {
            'total_cost': {
                'sense': 'minimize',
                'equations': [{'expression': 'p * cost'}],
            },
        },
    }


# ----------------------------------------------------------------------
# phases — each runs in its own process and is measured whole
# ----------------------------------------------------------------------


def _phase_interpreter(_cfg: dict[str, Any]) -> dict[str, Any]:
    """Bare interpreter: the floor nothing in this package can go below."""
    return {}


def _phase_import(_cfg: dict[str, Any]) -> dict[str, Any]:
    """``import farkas`` only. duckdb is a lazy import, so it is not here yet."""
    import farkas  # noqa: F401

    return {}


def _phase_connect(cfg: dict[str, Any]) -> dict[str, Any]:
    """Interpreter + duckdb loaded + a connection under the budget, no model.

    This is the constant the build peak has to be measured against: whatever
    it costs, ``memory_limit`` never gets a chance to influence it.
    """
    from farkas.relational.executor import DuckdbExecutor

    with (
        tempfile.TemporaryDirectory(dir=cfg['tmp_root']) as workdir,
        DuckdbExecutor(
            memory_limit=cfg['memory_limit'],
            chunk_rows=cfg['chunk_rows'],
            threads=cfg['threads'],
            workdir=workdir,
        ),
    ):
        return {}


def _phase_prep(cfg: dict[str, Any]) -> dict[str, Any]:
    """Write the model's inputs as parquet. Never a measured phase.

    Runs apart from every build so the numpy arrays behind the sources are
    resident in a process whose peak nobody reads.
    """
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    directory = Path(cfg['directory'])
    directory.mkdir(parents=True, exist_ok=True)
    generators, snapshots = cfg['generators'], cfg['snapshots']

    index = np.arange(generators)
    names = pa.array([f'g{i:04d}' for i in range(generators)], type=pa.string())
    p_max = np.where(index < round(generators * _MASKED_FRACTION), 0.0, 50.0 + index % 50)
    cost = 1.0 + (index % 37) * 0.5

    t = np.arange(snapshots, dtype=np.int64)
    # comfortably inside total capacity, so the model is feasible if solved
    load = 0.55 * float(p_max.sum()) * (1.0 + 0.25 * np.sin(t / 24.0))

    tables = {
        'p_max': pa.table({'generator': names, 'value': pa.array(p_max)}),
        'cost': pa.table({'generator': names, 'value': pa.array(cost)}),
        'load': pa.table({'snapshot': pa.array(t), 'value': pa.array(load)}),
        'snapshot': pa.table({'snapshot': pa.array(t)}),
    }
    paths = {}
    for name, table in tables.items():
        out = directory / f'{name}.parquet'
        pq.write_table(table, out)
        paths[name] = str(out)
    return {'sources': paths}


def _phase_build(cfg: dict[str, Any]) -> dict[str, Any]:
    """The measured phase: build the model, optionally drain it to an LP file."""
    import farkas as ly

    schema = dispatch_schema(cfg['generators'])
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(dir=cfg['tmp_root']) as workdir:
        with ly.build(
            schema,
            cfg['sources'],
            memory_limit=cfg['memory_limit'],
            chunk_rows=cfg['chunk_rows'],
            threads=cfg['threads'],
            workdir=workdir,
        ) as ex:
            build_seconds = time.perf_counter() - started
            extra: dict[str, Any] = {}
            if cfg['sink'] == 'lp':
                ex.write_lp(Path(workdir) / 'model.lp')
            elif cfg['sink'] == 'solve':
                # the point of comparison for the whole budget argument: what
                # the solver costs once it holds the model. Hard rule 4 exempts
                # this residency, so it is never in a build number — but it is
                # the ceiling a caller actually hits, and the build budget only
                # matters to the extent it is not dwarfed here.
                solved = time.perf_counter()
                result = ex.solve()
                extra = {
                    'solve_seconds': time.perf_counter() - solved,
                    'status': result.status,
                    'objective': result.objective,
                }
        return {'build_seconds': build_seconds, 'total_seconds': time.perf_counter() - started, **extra}


def _phase_build_eager(cfg: dict[str, Any]) -> dict[str, Any]:
    """The same model through the linopy lane, for the comparison that survives.

    The interesting claim in docs/benchmarks.md is comparative — streaming beats
    an eager build by enough to change what fits on a machine — and it is the
    one claim that does not depend on the budget being a bound. Measuring both
    arms in one harness on one box is what keeps it from resting on numbers
    nobody can reproduce.

    Ignores every budget knob: there is nothing to configure on this side, which
    is the point being made. ``sink`` is honoured though, and has to be: the
    original table's eager rows are labelled *lp-polars*, so they timed a build
    **and** an LP write, while its duckdb rows are the ones the 13.6x is quoted
    from. Comparing a build against a build-plus-write is how a ratio drifts,
    so both arms take the same sink here.
    """
    import pandas as pd
    import pyarrow.parquet as pq
    import yaml

    from farkas import linopy as farkas_linopy

    def column(name: str, dim: str) -> pd.Series:
        return pq.read_table(cfg['sources'][name]).to_pandas().set_index(dim)['value']

    data = {
        'p_max': column('p_max', 'generator'),
        'cost': column('cost', 'generator'),
        'load': column('load', 'snapshot'),
    }
    snapshots = pq.read_table(cfg['sources']['snapshot']).to_pandas()['snapshot']
    coords = {'snapshot': pd.Index(snapshots, name='snapshot')}

    with tempfile.TemporaryDirectory(dir=cfg['tmp_root']) as directory:
        path = Path(directory) / 'model.yaml'
        path.write_text(yaml.safe_dump(dispatch_schema(cfg['generators'])))
        started = time.perf_counter()
        model = farkas_linopy.build(path, data=data, coords=coords)
        build_seconds = time.perf_counter() - started
        if cfg['sink'] == 'lp':
            model.to_file(Path(directory) / 'model.lp', io_api='lp-polars')
        total_seconds = time.perf_counter() - started
        del model
    return {'build_seconds': build_seconds, 'total_seconds': total_seconds}


PHASES = {
    'interpreter': _phase_interpreter,
    'import': _phase_import,
    'connect': _phase_connect,
    'prep': _phase_prep,
    'build': _phase_build,
    'build_eager': _phase_build_eager,
}


# ----------------------------------------------------------------------
# parent: drive the children, then read the table it produced
# ----------------------------------------------------------------------


def run_child(phase: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Run one phase in a fresh process and return its record.

    A child that dies — duckdb raising rather than exceeding its budget is the
    documented failure mode — comes back as ``{'error': ...}`` so the sweep
    continues and the failure lands in the table where it belongs.
    """
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), '--child', phase, json.dumps(cfg)],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in proc.stdout.splitlines():
        if line.startswith(MARKER):
            return json.loads(line[len(MARKER) :])
    return {'error': _exception_line(proc.stderr or proc.stdout, proc.returncode), 'stderr': proc.stderr[-4000:]}


def _exception_line(output: str, returncode: int) -> str:
    """The line that says what went wrong, not the last line printed.

    duckdb's OOM message ends with a documentation URL, so the naive "tail -1"
    reports the footer and loses the operator that could not be spilled — which
    is the only part worth recording.
    """
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    for line in reversed(lines):
        if 'Error' in line or 'error' in line:
            return line
    return lines[-1] if lines else f'child exited {returncode} with no output'


def _mib(n: float) -> str:
    return f'{n / 1024**2:,.0f}'


def _parse_budget(text: str) -> float:
    """A duckdb memory-limit string as bytes, for the delta columns.

    **Decimal**, which is duckdb's own convention and not the obvious guess:
    it reports a ``128MB`` limit as ``122.0 MiB`` when it raises. Reading these
    as powers of two overstates every budget by 5% (MB) or 7% (GB), which is
    enough to turn "just under the limit" into "just over" in the table.
    """
    units = {'KB': 10**3, 'MB': 10**6, 'GB': 10**9, 'TB': 10**12}
    upper = text.strip().upper()
    for suffix, scale in units.items():
        if upper.endswith(suffix):
            return float(upper[: -len(suffix)].strip()) * scale
    return float(upper)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--snapshots', type=int, nargs='+', default=[100_000])
    parser.add_argument('--generators', type=int, default=100)
    parser.add_argument('--budgets', nargs='+', default=['128MB', '256MB', '512MB', '1GB', '2GB'])
    parser.add_argument('--chunk-rows', type=int, default=25_000)
    parser.add_argument('--threads', type=int, nargs='+', default=[0], help='0 = duckdb default')
    parser.add_argument('--sink', choices=['none', 'lp', 'solve'], default='none')
    parser.add_argument(
        '--eager', action='store_true', help='also build each size through the linopy lane ([linopy] extra)'
    )
    parser.add_argument('--data-dir', default=None, help='cache the generated parquet here')
    parser.add_argument('--tmp-root', default=None, help='parent of each build workdir (spill lives here)')
    parser.add_argument('--json', default=None, help='write the raw records here')
    args = parser.parse_args()

    owned_data_dir = args.data_dir is None
    data_root = Path(args.data_dir or tempfile.mkdtemp(prefix='farkas-bench-data-'))
    records: list[dict[str, Any]] = []

    try:
        print(f'# peak RSS sweep — {sys.platform}, python {sys.version.split()[0]}')
        print(
            f'# generators={args.generators} ({active_generators(args.generators)} active), chunk_rows={args.chunk_rows}, sink={args.sink}\n'
        )

        # ---------------- baselines ----------------
        print('## Baseline — what the budget never governs\n')
        print('| phase | peak RSS (MiB) |')
        print('|---|---:|')
        baseline_cfg = {'tmp_root': args.tmp_root, 'chunk_rows': args.chunk_rows, 'threads': None}

        for phase, label in (('interpreter', 'bare interpreter'), ('import', 'import farkas')):
            rec = run_child(phase, dict(baseline_cfg, memory_limit=args.budgets[0]))
            rec.update(phase=phase, label=label)
            records.append(rec)
            print(f'| {label} | {_mib(rec["peak_rss"]) if "peak_rss" in rec else rec["error"]} |')

        connect_peaks: dict[str, float] = {}
        for budget in args.budgets:
            rec = run_child('connect', dict(baseline_cfg, memory_limit=budget))
            rec.update(phase='connect', memory_limit=budget)
            records.append(rec)
            if 'peak_rss' in rec:
                connect_peaks[budget] = rec['peak_rss']
            shown = _mib(rec['peak_rss']) if 'peak_rss' in rec else rec['error']
            print(f'| connect @ {budget} | {shown} |')

        baseline = min(connect_peaks.values()) if connect_peaks else 0.0
        print(
            f'\nBaseline taken as the cheapest connect: **{_mib(baseline)} MiB**. '
            f'Everything below is measured against it.\n'
        )

        # ---------------- builds ----------------
        print('## Build\n')
        # "over budget" is the number that distinguishes a multiplier from a
        # fixed overhead, and they read very differently: ~250 MiB of untracked
        # allocation looks like 3x against a 128MB budget and like 1.06x against
        # 4GB. Negative means the build never needed the whole budget.
        print(
            '| snapshots | variables | threads | budget | peak RSS (MiB) | peak - baseline | over budget | vs budget | build s |'
        )
        print('|---:|---:|---:|---|---:|---:|---:|---:|---:|')

        for snapshots in args.snapshots:
            data_dir = data_root / f's{snapshots}_g{args.generators}'
            if not (data_dir / 'load.parquet').exists():
                prep = run_child(
                    'prep', {'directory': str(data_dir), 'snapshots': snapshots, 'generators': args.generators}
                )
                if 'error' in prep:
                    print(f'| {snapshots:,} | — | — | — | prep failed: {prep["error"]} | | | |')
                    continue
                sources = prep['sources']
            else:
                sources = {n: str(data_dir / f'{n}.parquet') for n in ('p_max', 'cost', 'load', 'snapshot')}

            variables = snapshots * active_generators(args.generators)

            if args.eager:
                rec = run_child(
                    'build_eager',
                    {
                        'sources': sources,
                        'generators': args.generators,
                        'tmp_root': args.tmp_root,
                        'sink': args.sink,
                    },
                )
                rec.update(phase='build_eager', snapshots=snapshots, generators=args.generators, variables=variables)
                records.append(rec)
                if 'error' in rec:
                    print(f'| {snapshots:,} | {variables:,} | — | *eager* | **{rec["error"][:60]}** | | | |')
                else:
                    print(
                        f'| {snapshots:,} | {variables:,} | — | *eager* | {_mib(rec["peak_rss"])} | '
                        f'{_mib(rec["peak_rss"] - baseline)} | n/a | n/a | {rec["build_seconds"]:.1f} |'
                    )

            for threads in args.threads:
                for budget in args.budgets:
                    cfg = {
                        'sources': sources,
                        'snapshots': snapshots,
                        'generators': args.generators,
                        'memory_limit': budget,
                        'chunk_rows': args.chunk_rows,
                        'threads': threads or None,
                        'sink': args.sink,
                        'tmp_root': args.tmp_root,
                    }
                    rec = run_child('build', cfg)
                    rec.update(
                        phase='build',
                        snapshots=snapshots,
                        generators=args.generators,
                        memory_limit=budget,
                        threads=threads or None,
                        variables=variables,
                        sink=args.sink,
                    )
                    records.append(rec)

                    thread_label = threads or 'auto'
                    if 'error' in rec:
                        print(
                            f'| {snapshots:,} | {variables:,} | {thread_label} | {budget} | **{rec["error"][:60]}** | | | |'
                        )
                        continue
                    peak = rec['peak_rss']
                    over = peak - baseline
                    excess = over - _parse_budget(budget)
                    ratio = peak / _parse_budget(budget)
                    print(
                        f'| {snapshots:,} | {variables:,} | {thread_label} | {budget} | {_mib(peak)} | '
                        f'{_mib(over)} | {"+" if excess >= 0 else "-"}{_mib(abs(excess))} | '
                        f'{ratio:.2f}x | {rec["build_seconds"]:.1f} |'
                    )

        # ---------------- verdict ----------------
        builds = [r for r in records if r.get('phase') == 'build' and 'peak_rss' in r]
        if builds:
            print('\n## Verdict\n')
            for snapshots in args.snapshots:
                rows = [r for r in builds if r['snapshots'] == snapshots]
                if len(rows) < 2:
                    continue
                lo, hi = min(rows, key=lambda r: r['peak_rss']), max(rows, key=lambda r: r['peak_rss'])
                budget_span = _parse_budget(hi['memory_limit']) / _parse_budget(lo['memory_limit'])
                peak_span = hi['peak_rss'] / lo['peak_rss']
                print(
                    f'- **{snapshots:,} snapshots** ({rows[0]["variables"]:,} vars): budget varied '
                    f'{max(budget_span, 1 / budget_span):.0f}x, peak varied {peak_span:.2f}x '
                    f'({_mib(lo["peak_rss"])} - {_mib(hi["peak_rss"])} MiB). '
                    f'Above baseline: {_mib(lo["peak_rss"] - baseline)} - {_mib(hi["peak_rss"] - baseline)} MiB.'
                )
            worst = max(builds, key=lambda r: r['peak_rss'] / _parse_budget(r['memory_limit']))
            print(
                f'- **Worst budget overshoot:** {worst["memory_limit"]} budget peaked at '
                f'{_mib(worst["peak_rss"])} MiB = {worst["peak_rss"] / _parse_budget(worst["memory_limit"]):.2f}x the limit.'
            )

        if args.json:
            Path(args.json).write_text(json.dumps(records, indent=2))
            print(f'\nRaw records: {args.json}')
    finally:
        if owned_data_dir:
            shutil.rmtree(data_root, ignore_errors=True)

    return 0


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--child':
        phase_name, raw_cfg = sys.argv[2], json.loads(sys.argv[3])
        result = PHASES[phase_name](raw_cfg)
        # measured last, after everything the phase allocated has been counted
        result['peak_rss'] = peak_rss_bytes()
        print(MARKER + json.dumps(result))
        raise SystemExit(0)
    raise SystemExit(main())
