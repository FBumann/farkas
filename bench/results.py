"""pytest-benchmark JSON in, flat measurement records out.

`bench/report.py` and `bench/plot.py` render the published tables and the chart
page, and between them they are four hundred lines of formatting that has been
read against the numbers it prints. When the harness became pytest, the cheap
and safe move was to leave every one of those lines alone and change only what
feeds them — so this module speaks the record shape they already read:

    {'record': 'timing', 'case', 'size', 'arm', 'sink',
     'wall_seconds', 'peak_rss_bytes', 'counts': {...}, 'live_fraction'}
    {'record': 'loop',   'case', 'size', 'arm',
     'first_build_seconds', 'steady_build_seconds'}
    {'record': 'run',    'platform', 'python', 'versions', 'commits'}

**Where each number comes from.** ``wall_seconds`` is pytest-benchmark's own
``min`` — repeats collapse by minimum because noise only ever adds, which is
the same rule the old runner applied by hand. ``peak_rss_bytes`` is the minimum
of pytest-benchmem's ``rss_bytes`` series, the whole-process high-water mark it
records under ``benchmem(isolate=True)`` and the only memory number honest
across two libraries (see `bench/test_ladder.py`). ``first`` and ``steady`` are
read off the per-round series: round 0 is the cold build, and the minimum of
the rest is what a rolling horizon pays.

**One thing the old runner did that this does not: record a failure as a
result.** It caught a child that died, kept the exception line, and the report
rendered it as a cell — which is how `docs/benchmarks.md` publishes that the
eager lane runs out of memory at a rung the relational one survives. Under
pytest a dead pass is an error, and a real OOM kills the process before
anything in it can record why. The readers still understand an ``error`` record
and will render one; nothing produces it yet. Until that is resolved, the top
rung is a claim made by a run that failed rather than by a table.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from bench.cases import CASES

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: pytest-benchmem's blob inside pytest-benchmark's ``extra_info``.
BENCHMEM = 'benchmem'


def _nominal(case: str | None, size: str | None) -> int | None:
    """The rung's declared width — a property of (case, rung), not of the run.

    Looked up rather than read back out of the result file: it is the x axis of
    every scaling table, and a file written by a run that forgot to record it
    would sort the tables by ``None``.
    """
    try:
        return CASES[case].shape(size).nominal_variables  # pyrefly: ignore[bad-argument-type]
    except (KeyError, TypeError):
        return None


def _commit(info: dict[str, Any]) -> str | None:
    head = (info.get('id') or '')[:7]
    if not head:
        return None
    return f'{head}-dirty' if info.get('dirty') else head


def _rss(extra: dict[str, Any]) -> float | None:
    """The whole-process peak, minimum across repeats. ``None`` without `isolate=True`."""
    series = (extra.get(BENCHMEM) or {}).get('rss_bytes')
    return min(float(v) for v in series) if series else None


def _counts(extra: dict[str, Any]) -> dict[str, Any]:
    return {k: extra.get(k) for k in ('columns', 'rows', 'nonzeros')}


def records(path: Path) -> Iterator[dict[str, Any]]:
    """Every measurement in *path*, in the shape the report and the plot read.

    Two formats, because the harness changed and the committed results did
    not. ``.jsonl`` is what the runner before #448 wrote — one record per
    line, already in this shape — and it is still what `bench/results/` holds,
    so the reader takes it verbatim. `.json` is pytest-benchmark's document,
    which the rest of this function unpacks.
    """
    if path.suffix == '.jsonl':
        for line in path.read_text().splitlines():
            if line.strip():
                yield json.loads(line)
        return

    doc = json.loads(path.read_text())
    machine, commit = doc.get('machine_info', {}), doc.get('commit_info', {})
    yield {
        'record': 'run',
        'platform': machine.get('system', '') + ' ' + machine.get('release', ''),
        'machine': machine.get('machine'),
        'processor': machine.get('processor') or machine.get('machine'),
        'python': machine.get('python_version'),
        # stamped by `pytest_benchmark_update_machine_info` in bench/conftest.py
        'versions': machine.get('versions', {}),
        # an editable install reports the version it was synced at rather than
        # the tree that ran, so the commit — dirty flag included — is what says
        # which working tree produced the number
        'commits': {'lpspec': _commit(commit)},
    }

    for b in doc.get('benchmarks', []):
        params, extra, stats = b.get('params') or {}, b.get('extra_info') or {}, b.get('stats') or {}
        common = {
            'case': params.get('case_name'),
            'size': params.get('size'),
            'arm': params.get('arm'),
            # the x of every scaling table. `columns` is what survived the
            # mask and is a different number.
            'nominal_variables': _nominal(params.get('case_name'), params.get('size')),
        }
        if b['name'].startswith('test_rebuild'):
            series = stats.get('data') or []
            yield {
                **common,
                'record': 'loop',
                'first_build_seconds': series[0] if series else None,
                # the cold build is round 0 and never competes with the rest —
                # the two numbers answer different questions
                'steady_build_seconds': min(series[1:]) if len(series) > 1 else None,
                'counts': _counts(extra),
            }
            continue
        yield {
            **common,
            'record': 'timing',
            'sink': params.get('sink'),
            'wall_seconds': stats.get('min'),
            'peak_rss_bytes': _rss(extra),
            'counts': _counts(extra),
            'live_fraction': extra.get('live_fraction'),
        }


def load(*paths: Path) -> list[dict[str, Any]]:
    """Flatten several result files, newest last — the readers take as many as given."""
    return [r for p in paths for r in records(p)]
