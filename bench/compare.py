"""Did *this change* move it? — two runs of the same arm, side by side.

    uv run python -m bench.run --cases dispatch --sizes m l --arms lpspec --repeat 3 --out /tmp/before.jsonl
    # ... make the change ...
    uv run python -m bench.run --cases dispatch --sizes m l --arms lpspec --repeat 3 --out /tmp/after.jsonl
    uv run python -m bench.compare /tmp/before.jsonl /tmp/after.jsonl

`bench.report` answers a different question — how two *arms* compare inside one
run — and cannot answer this one, because the two numbers being compared here
live in different files.

**The noise floor is the whole point.** A benchmark that prints "-6%" without
saying whether -6% is distinguishable from nothing is worse than no benchmark:
it invites a claim the numbers do not support. So each side's repeats give a
spread, and a delta smaller than the wider of the two is reported as *noise*
rather than as an improvement. Run with ``--repeat 3`` or more if you want that
verdict to mean much; with ``--repeat 1`` there is no spread to measure and
every delta is reported unqualified, which is stated rather than hidden.

Peaks and walls are read the way the harness records them: the *minimum* across
repeats, because noise only ever adds.

**Run the two back to back, on an idle machine.** The spread this computes is
*within* a run and says nothing about drift *between* them — so a baseline
recorded before doing something else, and an "after" recorded once the laptop
is warm, will disagree for reasons that have nothing to do with the change.
Neither of those is hypothetical. Writing this module, a revert measured +22.9%
`WORSE` against a baseline taken minutes earlier, purely because a test suite
had run in between; re-measured back to back it was +0.3%. And on a machine
already running another benchmark, two runs of *identical* code reported -42.7%
`better` — clearing a 33.8% floor, because the within-run spread cannot see load
that drifts across runs.

So the verdict column is only as good as the conditions, and no floor computed
from a single pair can fix that. If a result matters: nothing else running,
baseline re-recorded immediately before the comparison, and if it is close,
run the pair again the other way round.

**Compare phases, not `wall`.** The default is `build` and `peak`, which is what
engine work moves. Total wall is reported for context and is a poor detector:
measured on `dispatch/m`, doubling every query showed as +18.7% wall against a
21.7% noise floor — invisible — and as +21.7% build against a 6.7% floor, which
is unambiguous. The difference is `emit`, whose spread reached 67% on the same
runs and swamps everything it is summed with.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

Key = tuple[str, str, str, str]

#: Everything comparable. `build` and `peak` are the default because they are
#: what an engine change moves; `wall` sums in `emit`, which is the noisy part.
METRICS = ('build', 'peak', 'wall', 'emit')
DEFAULT_METRICS = ('build', 'peak')


def _timings(path: Path) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in records if r.get('record') == 'timing' and 'error' not in r]


def _collect(path: Path) -> dict[Key, dict[str, list[float]]]:
    """(case, size, sink, arm) -> every repeat of every metric."""
    out: dict[Key, dict[str, list[float]]] = {}
    for r in _timings(path):
        key = (r['case'], r['size'], r.get('sink', 'lp'), r['arm'])
        got = out.setdefault(key, {m: [] for m in METRICS})
        got['wall'].append(r['wall_seconds'])
        got['peak'].append(r['peak_rss_bytes'])
        for phase in ('build', 'emit'):
            if phase in r.get('phases', {}):
                got[phase].append(r['phases'][phase])
    return out


def _spread(values: list[float]) -> float:
    """How far the repeats of one measurement disagree, as a fraction of the best.

    The floor a delta has to clear to be worth reporting. **The first repeat is
    dropped** when there are enough left to measure without it: a cold cache is
    systematic, not noise, and counting it puts the floor above every regression
    worth finding — 194% on polars' build phase, where the honest figure is 15%.

    One repeat has no spread to measure and returns 0, which makes every delta
    look significant; the caller says `unrepeated` rather than implying a result.
    """
    usable = values[1:] if len(values) >= 3 else values
    if len(usable) < 2:
        return 0.0
    lo = min(usable)
    return (max(usable) - lo) / lo if lo else 0.0


def _verdict(delta: float, floor: float) -> str:
    if floor == 0.0:
        return 'unrepeated'
    if abs(delta) <= floor:
        return 'noise'
    return 'better' if delta < 0 else 'WORSE'


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('before', type=Path)
    ap.add_argument('after', type=Path)
    ap.add_argument(
        '--metric',
        nargs='+',
        default=list(DEFAULT_METRICS),
        choices=(*METRICS, 'all'),
        help='default: build peak. `wall` and `emit` are available; wall is a poor detector '
        'because emit dominates its noise. They can move in opposite directions.',
    )
    opts = ap.parse_args(argv)

    before, after = _collect(opts.before), _collect(opts.after)
    shared = sorted(set(before) & set(after))
    if not shared:
        print(f'nothing in common between {opts.before.name} and {opts.after.name}')
        only_b, only_a = sorted(set(before) - set(after)), sorted(set(after) - set(before))
        if only_b or only_a:
            print(f'  before has {[k[:3] for k in only_b[:4]]}\n  after has  {[k[:3] for k in only_a[:4]]}')
        return 1

    metrics = METRICS if 'all' in opts.metric else tuple(opts.metric)
    width = max(len(f'{c}/{s} {k} {a}') for c, s, k, a in shared)
    header = f'{"case/size sink arm".ljust(width)}'
    for m in metrics:
        header += f'  {m + " before":>13} {m + " after":>13} {"delta":>8} {"±noise":>7} {"":>10}'
    print(header)
    print('-' * len(header))

    regressed = 0
    for key in shared:
        case, size, sink, arm = key
        line = f'{f"{case}/{size} {sink} {arm}".ljust(width)}'
        for m in metrics:
            b, a = min(before[key][m]), min(after[key][m])
            delta = (a - b) / b if b else 0.0
            floor = max(_spread(before[key][m]), _spread(after[key][m]))
            verdict = _verdict(delta, floor)
            regressed += verdict == 'WORSE'
            fmt = (lambda v: f'{v / 1e6:.0f} MB') if m == 'peak' else (lambda v: f'{v:.3f} s')
            line += f'  {fmt(b):>13} {fmt(a):>13} {delta * 100:>+7.1f}% {floor * 100:>6.1f}% {verdict:>10}'
        print(line)

    print()
    print('delta = after vs before, minimum across repeats. ±noise = the wider of the two spreads;')
    print('a delta inside it is reported as noise. `unrepeated` means --repeat 1, so nothing is known.')
    if regressed:
        print(f'\n{regressed} measurement(s) got worse by more than the noise floor.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
