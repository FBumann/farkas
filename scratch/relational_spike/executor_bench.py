"""Scale check: the general executor vs the hand-written spike SQL.

Runs the dispatch program (parquet sources) through DuckdbExecutor at
S x G scale, timing build / write_lp / solve separately.

    /usr/bin/time -l .venv/bin/python scratch/relational_spike/executor_bench.py \
        --data scratch/relational_spike/bench_out/data_s100000_g100 --memory-limit 512MB
"""

import argparse
import time
from pathlib import Path

from linopy_yaml.relational import (
    Cmp,
    Const,
    ConstraintDecl,
    DuckdbExecutor,
    ObjectiveDecl,
    Param,
    ParameterDecl,
    Program,
    Sum,
    Var,
    VariableDecl,
)

HERE = Path(__file__).parent


def dispatch_program(trivial: bool = False) -> Program:
    """The dispatch model; with ``trivial=True`` every variable is fixed
    (lower == upper == p_fix, chosen to satisfy the balance exactly), so the
    solver's presolve finishes the model instantly and a benchmark measures
    build + streaming only, not simplex time."""
    if trivial:
        bounds = {'lower': Param('p_fix'), 'upper': Param('p_fix')}
        extra = (ParameterDecl('p_fix', ('snapshot', 'generator')),)
    else:
        bounds = {'lower': Const(0.0), 'upper': Param('p_max')}
        extra = ()
    return Program(
        parameters=(
            ParameterDecl('p_max', ('generator',)),
            ParameterDecl('cost', ('generator',)),
            ParameterDecl('load', ('snapshot',)),
            *extra,
        ),
        variables=(
            VariableDecl(
                'p',
                ('snapshot', 'generator'),
                where=Cmp('p_max', '>', 0),
                **bounds,
            ),
        ),
        constraints=(
            ConstraintDecl(
                'power_balance',
                ('snapshot',),
                lhs=Sum(Var('p'), over=('generator',)),
                sense='==',
                rhs=Param('load'),
            ),
        ),
        objective=ObjectiveDecl('min', Sum(Var('p') * Param('cost'), over=('generator', 'snapshot'))),
    )


def main() -> None:
    import duckdb

    ap = argparse.ArgumentParser()
    ap.add_argument('--data', type=Path, required=True)
    ap.add_argument('--memory-limit', default='512MB')
    ap.add_argument('--out', type=Path, default=HERE / 'bench_out' / 'executor.lp')
    ap.add_argument('--solve', action='store_true', help='also run the solver_direct sink')
    ap.add_argument(
        '--trivial',
        action='store_true',
        help='fix all variables so presolve solves instantly (measures streaming only)',
    )
    args = ap.parse_args()

    # the executor wants tidy (dims..., value) sources; adapt the spike parquet
    con = duckdb.connect()
    prep = args.data / 'prep'
    prep.mkdir(exist_ok=True)
    con.execute(
        f"COPY (SELECT generator, p_max AS value FROM read_parquet('{args.data}/generators.parquet')) "
        f"TO '{prep}/p_max.parquet' (FORMAT parquet)"
    )
    con.execute(
        f"COPY (SELECT generator, cost AS value FROM read_parquet('{args.data}/generators.parquet')) "
        f"TO '{prep}/cost.parquet' (FORMAT parquet)"
    )
    con.execute(
        f"COPY (SELECT snapshot, load AS value FROM read_parquet('{args.data}/load.parquet')) "
        f"TO '{prep}/load.parquet' (FORMAT parquet)"
    )

    sources = {
        'p_max': str(prep / 'p_max.parquet'),
        'cost': str(prep / 'cost.parquet'),
        'load': str(prep / 'load.parquet'),
        'snapshot': str(prep / 'load.parquet'),
    }

    if args.trivial:
        # proportional dispatch: p_fix = load * p_max / sum(active p_max),
        # so sum(p_fix) == load and the balance holds with all vars fixed
        con.execute(
            f"""
            COPY (
                SELECT l.snapshot, g.generator,
                       l.load * g.p_max / (
                           SELECT SUM(p_max) FROM read_parquet('{args.data}/generators.parquet')
                           WHERE p_max > 0
                       ) AS value
                FROM read_parquet('{args.data}/load.parquet') l
                CROSS JOIN read_parquet('{args.data}/generators.parquet') g
                WHERE g.p_max > 0
            ) TO '{prep}/p_fix.parquet' (FORMAT parquet)
            """
        )
        sources['p_fix'] = str(prep / 'p_fix.parquet')
    con.close()

    with DuckdbExecutor(memory_limit=args.memory_limit) as ex:
        t0 = time.perf_counter()
        ex.build(dispatch_program(trivial=args.trivial), sources)
        t1 = time.perf_counter()
        print(f'build:    {t1 - t0:6.2f}s  ({ex._n_cols:,} cols, {ex._n_rows:,} rows)')

        ex.write_lp(args.out)
        t2 = time.perf_counter()
        print(f'write_lp: {t2 - t1:6.2f}s  ({args.out.stat().st_size / 1e6:.1f} MB)')

        if args.solve:
            sol = ex.solve()
            t3 = time.perf_counter()
            print(f'solve:    {t3 - t2:6.2f}s  status={sol.status} obj={sol.objective:.6g}')


if __name__ == '__main__':
    main()
