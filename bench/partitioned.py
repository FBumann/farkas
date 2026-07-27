"""PROTOTYPE — not for merge. Does partition-wise assembly bound polars' peak?

The question this settles: farkas's terminal aggregate groups by ``(row, col)``,
a near-unique key, so the query does not *reduce* — its output is the model.
No engine setting can bound a result you asked for in full, which is why nothing
spills under `POLARS_OOC_MEMORY_BUDGET_MB` and why duckdb's advantage was really
"the result lives in a file-backed database, not in the process".

The only bound available to polars is therefore to never hold the result whole:
assemble the matrix a row-range at a time and drain each block. `row` is the
leading group key, so blocks are independent.

The cost is re-joining the full variable frame per block — O(blocks x variables)
against O(1) — so this measures both axes. If it lands near duckdb's peak at
polars' speed, polars wins outright; if it lands at duckdb's speed too, it is
duckdb with extra steps.

    uv run python -m bench.partitioned --case dispatch --size l
"""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MARKER = '##PART##'
_RSS_UNIT = 1 if sys.platform == 'darwin' else 1024


def peak_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _RSS_UNIT / 1024**2


def _build(case_name: str, size: str, partitioned: bool):
    """`api.build`, with the prototype flag reachable."""
    from bench._run_case import _split_sources
    from bench.cases import CASES
    from farkas.api import load_schema
    from farkas.lowering import lower_program
    from farkas.relational.executor import PolarsExecutor
    from farkas.sources import tidy_sources

    case = CASES[case_name]
    sources, coords = _split_sources(case, case.data(case.shape(size)))
    schema = load_schema(case.model)
    program = lower_program(schema)
    ex = PolarsExecutor(partitioned=partitioned)
    ex.build(program, tidy_sources(schema, dict(sources), coords))
    return ex


def write_lp_partitioned(ex, path: Path, rows_per_block: int) -> None:
    """The LP file, with the constraint section assembled block by block.

    Reuses the shipped sink for every section but the constraints, so only the
    one thing under test differs — and the bytes are asserted identical to the
    whole-matrix path in ``--verify``.
    """
    import polars as pl

    # built inline rather than via `_tables()`, which asserts a matrix this
    # mode deliberately never produces
    from farkas.relational.sinks import ModelTables
    from farkas.relational.sinks import lp_file as sink

    tables = ModelTables(
        cols=ex._cols,
        obj=ex._obj,
        rows=ex._rows,
        matrix=ex._rows.head(0)
        .select('row')
        .with_columns(pl.lit(0, dtype=pl.Int64).alias('col'), pl.lit(0.0).alias('coeff')),
        column_count=ex._n_cols,
        row_count=ex._n_rows,
        objective_sense=ex._obj_sense,
        objective_constant=ex._obj_const,
    )
    rows = tables.rows.sort('row')
    with open(path, 'wb') as f:
        f.write((b'min' if tables.objective_sense == 'min' else b'max') + b'\n\nobj:\n')
        if tables.objective_constant:
            f.write(f'{tables.objective_constant:+.17g}\n'.encode())
        sink._sink(tables.obj.lazy().sort('col').select(sink._term(pl.col('coeff'), pl.col('col'))), f)

        f.write(b'\ns.t.\n\n')
        for lo, hi, block in ex.matrix_blocks(rows_per_block):
            window = rows.filter(pl.col('row').is_between(lo, hi, closed='left')).lazy()
            header = window.select(
                'row',
                pl.lit(-2, dtype=pl.Int64).alias('ord'),
                pl.concat_str(pl.lit('c'), sink._digits(pl.col('row')), pl.lit(':')).alias('line'),
            )
            placeholder = window.join(block.lazy().select('row').unique(), on='row', how='anti').select(
                'row', pl.lit(-1, dtype=pl.Int64).alias('ord'), pl.lit('+0 x0').alias('line')
            )
            terms = block.lazy().select(
                'row',
                pl.col('col').cast(pl.Int64).alias('ord'),
                sink._term(pl.col('coeff'), pl.col('col')).alias('line'),
            )
            footer = window.select(
                'row',
                pl.lit(2**62, dtype=pl.Int64).alias('ord'),
                pl.concat_str(pl.col('sense').replace({'==': '='}), pl.lit(' '), sink._number(pl.col('rhs'))).alias(
                    'line'
                ),
            )
            sink._sink(pl.concat([header, placeholder, terms, footer]).sort('row', 'ord').select('line'), f)

        f.write(b'\nbounds\n')
        bounds = (
            tables.cols.lazy()
            .sort('col')
            .select(
                pl.concat_str(
                    sink._bound(pl.col('lb'), '-infinity'),
                    pl.lit(' <= x'),
                    sink._digits(pl.col('col')),
                    pl.lit(' <= '),
                    sink._bound(pl.col('ub'), '+infinity'),
                )
            )
        )
        sink._sink(bounds, f)
        for variable_type, keyword in (('binary', 'binary'), ('integer', 'general')):
            chosen = tables.cols.lazy().filter(pl.col('vtype') == variable_type).sort('col')
            if chosen.select(pl.len()).collect().item() == 0:
                continue
            f.write(f'\n{keyword}\n'.encode())
            sink._sink(chosen.select(pl.concat_str(pl.lit('x'), sink._digits(pl.col('col')))), f)
        f.write(b'\nend\n')


def _child(case_name: str, size: str, mode: str, rows_per_block: int) -> None:
    partitioned = mode == 'partitioned'
    started = time.perf_counter()
    ex = _build(case_name, size, partitioned)
    build_seconds = time.perf_counter() - started
    with tempfile.TemporaryDirectory(prefix='farkas-part-') as tmp:
        out = Path(tmp) / 'model.lp'
        if partitioned:
            write_lp_partitioned(ex, out, rows_per_block)
        else:
            ex.write_lp(out)
        size_bytes = out.stat().st_size
    ex.close()
    print(
        MARKER
        + json.dumps(
            {
                'peak': peak_mib(),
                'build_seconds': build_seconds,
                'total_seconds': time.perf_counter() - started,
                'bytes': size_bytes,
            }
        )
    )


def _run(case_name: str, size: str, mode: str, rows_per_block: int) -> dict | str:
    proc = subprocess.run(
        [sys.executable, '-m', 'bench.partitioned', '--child', case_name, size, mode, str(rows_per_block)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    record = next((json.loads(x[len(MARKER) :]) for x in proc.stdout.splitlines() if x.startswith(MARKER)), None)
    if record is None:
        tail = (proc.stderr or proc.stdout).strip().splitlines()
        return tail[-1][:90] if tail else 'no output'
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', nargs='+', default=['dispatch', 'transport'])
    parser.add_argument('--size', default='l')
    parser.add_argument('--rows-per-block', type=int, nargs='+', default=[5000, 25000])
    args = parser.parse_args()

    print(f'# partition-wise assembly — {args.size} rung\n')
    print('| case | mode | peak RSS (MiB) | build s | total s | LP bytes |')
    print('|---|---|---:|---:|---:|---:|')
    for case_name in args.cases:
        whole = _run(case_name, args.size, 'whole', 0)
        if isinstance(whole, str):
            print(f'| {case_name} | whole matrix | **{whole}** | | | |')
        else:
            print(
                f'| {case_name} | whole matrix | {whole["peak"]:,.0f} | {whole["build_seconds"]:.1f} | '
                f'{whole["total_seconds"]:.1f} | {whole["bytes"] / 1024**2:,.0f} MB |'
            )
        for rows_per_block in args.rows_per_block:
            part = _run(case_name, args.size, 'partitioned', rows_per_block)
            if isinstance(part, str):
                print(f'| {case_name} | {rows_per_block:,} rows/block | **{part}** | | | |')
                continue
            print(
                f'| {case_name} | {rows_per_block:,} rows/block | {part["peak"]:,.0f} | '
                f'{part["build_seconds"]:.1f} | {part["total_seconds"]:.1f} | '
                f'{part["bytes"] / 1024**2:,.0f} MB |'
            )
    return 0


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--child':
        _child(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]))
        raise SystemExit(0)
    raise SystemExit(main())
