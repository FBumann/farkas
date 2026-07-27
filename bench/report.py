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
import re
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
Key = tuple[str, str, str]


def _key(r: Row) -> Key:
    return (r['case'], r['size'], r['arm'])


def best(timings: list[Row]) -> dict[Key, Row]:
    """(case, size, arm) -> the fastest repeat."""
    out: dict[Key, Row] = {}
    for r in timings:
        if 'error' in r:
            continue
        key = _key(r)
        if key not in out or r['wall_seconds'] < out[key]['wall_seconds']:
            out[key] = r
    return out


def failures(timings: list[Row]) -> dict[Key, str]:
    """A run that died is a measurement, and the report renders it as one."""
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


_DENSITY_RUNG = re.compile(r'd\d+$')


def sizes_of(case: str, rows: dict[Key, Row], *, density: bool = False) -> list[str]:
    """Rung labels for *case*, smallest model first.

    The density sweep is held at one model size, so mixing it into the size
    ladder would sort four densities in among four sizes and read as a single
    monotone column that is really two axes. They get separate tables.
    """
    seen = {
        s: r['counts']['columns']
        for (c, s, _), r in rows.items()
        if c == case and bool(_DENSITY_RUNG.match(s)) == density
    }
    return sorted(seen, key=lambda s: seen[s])


def table(case: str, rows: dict[Key, Row]) -> str:
    lines = [
        f'### {case}',
        '',
        'farkas on its relational engine, linopy through its `lp-polars` writer.',
        '',
        '| variables | live | rows | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak | LP | scratch |',
        '|---|---|---|---|---|---|---|---|---|---|---|',
    ]
    for size in sizes_of(case, rows):
        arms = {'farkas': rows.get((case, size, 'farkas')), 'linopy': rows.get((case, size, 'linopy'))}
        ref = next((r for r in arms.values() if r), None)
        if ref is None:
            continue
        wall = {a: (r['wall_seconds'] if r else None) for a, r in arms.items()}
        peak = {a: (r['peak_rss_bytes'] if r else None) for a, r in arms.items()}
        scratch = (arms['farkas'] or {}).get('workdir_bytes')
        cells = [
            _si(ref['counts']['columns']),
            _live(ref),
            _si(ref['counts']['rows']),
            *(f'{wall[a]:.2f} s' if wall[a] else '—' for a in ARMS),
            _ratio(wall['farkas'], wall['linopy']),
            *(f'{_gb(peak[a])} GB' if peak[a] else '—' for a in ARMS),
            _ratio(peak['farkas'], peak['linopy']),
            f'{ref["lp_bytes"] / 1e6:.0f} MB',
            f'{scratch / 1e9:.2f} GB' if scratch else '—',
        ]
        lines.append('| ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines)


def _live(r: Row) -> str:
    """What fraction of the coordinate product survived the mask.

    Reported rather than assumed. `dispatch` declares `where: p_max > 0` and
    keeps 100% of its product — the engine pays for a mask that removes
    nothing, and that only shows up if the harness measures it.
    """
    frac = r.get('live_fraction')
    return '—' if frac is None else f'{frac * 100:.0f}%'


def density(rows: dict[Key, Row]) -> str:
    """One model size, four mask densities — the axis the ladder cannot show.

    A mask is row absence relationally and a NaN-padded dense array eagerly, so
    this is the one comparison where the two lanes are not doing the same work
    in different orders — they are doing different amounts of work.
    """
    cases = [c for c in sorted({c for c, _, _ in rows}) if sizes_of(c, rows, density=True)]
    if not cases:
        return ''
    lines = [
        '### The mask sweep',
        '',
        'One model size. For `nodal`, `live` is how many of the 12 technologies '
        'each node has installed: 12 / 6 / 3 / 1.',
        '',
        '| case | live | variables | wall: farkas | wall: linopy | wall | peak: farkas | peak: linopy | peak |',
        '|---|---|---|---|---|---|---|---|---|',
    ]
    for case in cases:
        for size in reversed(sizes_of(case, rows, density=True)):
            arms = {'farkas': rows.get((case, size, 'farkas')), 'linopy': rows.get((case, size, 'linopy'))}
            ref = next((r for r in arms.values() if r), None)
            if ref is None:
                continue
            wall = {a: (r['wall_seconds'] if r else None) for a, r in arms.items()}
            peak = {a: (r['peak_rss_bytes'] if r else None) for a, r in arms.items()}
            cells = [
                case,
                _live(ref),
                _si(ref['counts']['columns']),
                *(f'{wall[a]:.2f} s' if wall[a] else '—' for a in ARMS),
                _ratio(wall['farkas'], wall['linopy']),
                *(f'{_gb(peak[a])} GB' if peak[a] else '—' for a in ARMS),
                _ratio(peak['farkas'], peak['linopy']),
            ]
            lines.append('| ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('results', type=Path, nargs='*', default=[Path('bench/results/latest.jsonl')])
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
    for case in sorted({c for c, _, _ in rows}):
        print()
        print(table(case, rows))
    density_table = density(rows)
    if density_table:
        print()
        print(density_table)
    for key, error in sorted(failed.items()):
        print(f'\n<!-- {" ".join(k for k in key if k)}: {error} -->')
    return 0


if __name__ == '__main__':
    sys.exit(main())
