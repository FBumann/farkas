"""Phase-1 gate benchmark: peak memory + runtime, linopy eager writer vs duckdb streaming.

Uses pytest-benchmem's measure_memory (memray under the hood — never tracemalloc)
with isolate=True, so every pass runs in a fresh spawned process and also reports
the child's whole-process peak RSS (ru_maxrss). Peak RSS is the gate metric; the
memray allocator peak is the attributable detail.

Run:
    uv run --group spike python scratch/relational_spike/bench.py \
        --snapshots 100000 --generators 100 --repeats 3
"""

import argparse
import functools
import time
from pathlib import Path

HERE = Path(__file__).parent


def run_linopy(data_dir: Path, out: Path, io_api: str) -> None:
    from linopy_baseline import build_and_write

    build_and_write(data_dir, out, io_api)


def run_duckdb(data_dir: Path, out: Path, memory_limit: str) -> None:
    from duckdb_spike import build_and_write

    build_and_write(data_dir, out, memory_limit)


def fmt_bytes(n: int | None) -> str:
    return '-' if n is None else f'{n / 2**20:,.0f} MiB'


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--snapshots', type=int, default=100_000)
    ap.add_argument('--generators', type=int, default=100)
    ap.add_argument('--repeats', type=int, default=3)
    ap.add_argument('--memory-limit', default='1GB', help='duckdb memory_limit')
    ap.add_argument('--io-api', default='lp-polars', choices=['lp', 'lp-polars'])
    ap.add_argument('--workdir', type=Path, default=HERE / 'bench_out')
    args = ap.parse_args()

    from gen_data import main as _  # noqa: F401  (import check only)
    from pytest_benchmem import measure_memory

    args.workdir.mkdir(parents=True, exist_ok=True)
    data_dir = args.workdir / f'data_s{args.snapshots}_g{args.generators}'
    if not (data_dir / 'generators.parquet').exists():
        import subprocess
        import sys

        subprocess.run(
            [
                sys.executable,
                str(HERE / 'gen_data.py'),
                '--snapshots',
                str(args.snapshots),
                '--generators',
                str(args.generators),
                '--out',
                str(data_dir),
            ],
            check=True,
        )

    cases = {
        f'linopy/{args.io_api}': functools.partial(run_linopy, data_dir, args.workdir / 'model_linopy.lp', args.io_api),
        f'duckdb/{args.memory_limit}': functools.partial(
            run_duckdb, data_dir, args.workdir / 'model_duckdb.lp', args.memory_limit
        ),
    }

    results = {}
    for name, action in cases.items():
        t0 = time.perf_counter()
        res = measure_memory(action, repeats=args.repeats, warmup=0, isolate=True)
        wall = (time.perf_counter() - t0) / max(args.repeats, 1)
        results[name] = (res, wall)

    print(f'\n=== dispatch S={args.snapshots:,} G={args.generators} (repeats={args.repeats}) ===')
    print(f'{"case":<24} {"peak RSS":>12} {"memray peak":>12} {"~wall/pass":>12}')
    for name, (res, wall) in results.items():
        print(f'{name:<24} {fmt_bytes(res.rss_bytes):>12} {fmt_bytes(res.peak_bytes):>12} {wall:>10.1f}s')


if __name__ == '__main__':
    main()
