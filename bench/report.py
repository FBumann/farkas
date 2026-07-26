"""Turn a results JSONL into the markdown that goes in docs/benchmarks.md.

    uv run python -m bench.report bench/results/latest.jsonl

Nothing here recomputes or smooths anything: repeats collapse by *minimum*,
which is the usual choice for a benchmark because noise only ever adds. The
point of this module existing at all is that the published table has one
provenance — a file — instead of being retyped by hand and then outliving the
harness that produced it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ARMS = ('farkas', 'linopy')


def load(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    run = next((r for r in records if r.get('record') == 'run'), {})
    gates = [r for r in records if r.get('record') == 'gate']
    timings = [r for r in records if r.get('record') == 'timing']
    return run, gates, timings


Row = dict[str, Any]
Key = tuple[str, str, str, str]


def _key(r: Row) -> Key:
    return (r['case'], r['size'], r['arm'], r.get('memory_limit') or '')


def best(timings: list[Row]) -> dict[Key, Row]:
    """(case, size, arm, budget) -> the fastest repeat."""
    out: dict[Key, Row] = {}
    for r in timings:
        if 'error' in r:
            continue
        key = _key(r)
        if key not in out or r['wall_seconds'] < out[key]['wall_seconds']:
            out[key] = r
    return out


def failures(timings: list[Row]) -> dict[Key, str]:
    """A run that died is a measurement: an OOM names the budget it needed."""
    return {_key(r): r['error'] for r in timings if 'error' in r}


def _si(n: float) -> str:
    for unit, scale in (('M', 1e6), ('k', 1e3)):
        if n >= scale:
            return f'{n / scale:.6g}{unit}'
    return f'{n:.0f}'


def _gb(n: float) -> str:
    return f'{n / 1e9:.2f}'


def _ratio(a: float | None, b: float | None) -> str:
    return f'{a / b:.2f}x' if a and b else '—'


def sizes_of(case: str, rows: dict[Key, Row]) -> list[str]:
    """Rung labels for *case*, smallest model first."""
    seen = {s: r['counts']['columns'] for (c, s, _, _), r in rows.items() if c == case}
    return sorted(seen, key=lambda s: seen[s])


def table(case: str, rows: dict[Key, Row], budget: str) -> str:
    lines = [
        f'### {case}',
        '',
        f'farkas at `memory_limit={budget}`, linopy through its `lp-polars` writer.',
        '',
        '| variables | rows | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak | LP |',
        '|---|---|---|---|---|---|---|---|---|',
    ]
    for size in sizes_of(case, rows):
        arms = {'farkas': rows.get((case, size, 'farkas', budget)), 'linopy': rows.get((case, size, 'linopy', ''))}
        ref = next((r for r in arms.values() if r), None)
        if ref is None:
            continue
        wall = {a: (r['wall_seconds'] if r else None) for a, r in arms.items()}
        peak = {a: (r['peak_rss_bytes'] if r else None) for a, r in arms.items()}
        cells = [
            _si(ref['counts']['columns']),
            _si(ref['counts']['rows']),
            *(f'{wall[a]:.2f} s' if wall[a] else '—' for a in ARMS),
            _ratio(wall['farkas'], wall['linopy']),
            *(f'{_gb(peak[a])} GB' if peak[a] else '—' for a in ARMS),
            _ratio(peak['farkas'], peak['linopy']),
            f'{ref["lp_bytes"] / 1e6:.0f} MB',
        ]
        lines.append('| ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines)


def knob(rows: dict[Key, Row], failed: dict[Key, str]) -> str:
    """Peak RSS against the configured budget — the claim, in one table."""
    budgets = sorted({b for (_, _, arm, b) in [*rows, *failed] if arm == 'farkas' and b}, key=_bytes)
    if len(budgets) < 2:
        return ''
    header = '| case | variables | ' + ' | '.join(f'`{b}`' for b in budgets) + ' | linopy |'
    lines = ['### peak RSS against the budget', '', header, '|---' * (len(budgets) + 3) + '|']
    for case in sorted({c for c, _, _, _ in rows}):
        for size in sizes_of(case, rows):
            measured = [rows.get((case, size, 'farkas', b)) for b in budgets]
            if not any(measured):
                continue
            eager = rows.get((case, size, 'linopy', ''))
            ref = next(r for r in measured if r)
            cells = [
                case,
                _si(ref['counts']['columns']),
                *(
                    f'{_gb(r["peak_rss_bytes"])} GB'
                    if r
                    else ('**OOM**' if (case, size, 'farkas', b) in failed else '—')
                    for r, b in zip(measured, budgets, strict=True)
                ),
                f'{_gb(eager["peak_rss_bytes"])} GB' if eager else '—',
            ]
            lines.append('| ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines)


def _bytes(budget: str) -> float:
    scale = {'KB': 1e3, 'MB': 1e6, 'GB': 1e9, 'TB': 1e12}
    return float(budget[:-2]) * scale.get(budget[-2:].upper(), 1.0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('results', type=Path, nargs='*', default=[Path('bench/results/latest.jsonl')])
    ap.add_argument('--budget', default='1GB', help='which farkas budget the headline table reports')
    opts = ap.parse_args(argv)

    run: dict[str, Any] = {}
    gates: list[Row] = []
    timings: list[Row] = []
    for path in opts.results:
        one_run, one_gates, one_timings = load(path)
        run = run or one_run
        gates += one_gates
        timings += one_timings
    rows = best(timings)
    failed = failures(timings)

    versions = ', '.join(f'{k} {v}' for k, v in run.get('versions', {}).items() if v)
    print(f'{run.get("platform", "?")}, python {run.get("python", "?")} — {versions}.')
    print()
    print(
        'Parity gate: '
        + '; '.join(
            f'{g["case"]} objectives agree to {g["relative_gap"]:.1e} relative'
            if g['passed']
            else f'{g["case"]} FAILED'
            for g in gates
        )
        + '.'
    )
    for case in sorted({c for c, _, _, _ in rows}):
        print()
        print(table(case, rows, opts.budget))
    knob_table = knob(rows, failed)
    if knob_table:
        print()
        print(knob_table)
    for key, error in sorted(failed.items()):
        print(f'\n<!-- {" ".join(k for k in key if k)}: {error} -->')
    return 0


if __name__ == '__main__':
    sys.exit(main())
