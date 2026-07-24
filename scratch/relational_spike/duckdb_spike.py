"""Relational backend spike: hand-translated dispatch model as duckdb SQL over parquet.

The mapping under test (see handoff / SPEC direction):
  - expressions -> tidy tables (coord_cols..., var_label, coeff)
  - masks       -> row absence (WHERE p_max > 0), no NaN sentinels
  - broadcast   -> joins (loads CROSS JOIN active generators)
  - sum(over=g) -> GROUP BY snapshot
  - labels      -> ROW_NUMBER() over the masked coord product
  - LP writing  -> streaming COPY per section under a duckdb memory_limit,
                   sections concatenated at the end

The duckdb database is file-backed so the buffer pool respects memory_limit and
spills to disk instead of ballooning RSS.
"""

import argparse
import shutil
import time
from pathlib import Path

COPY_OPTS = "(FORMAT csv, HEADER false, QUOTE '', ESCAPE '')"


def build_and_write(
    data_dir: Path,
    out: Path,
    memory_limit: str = '1GB',
    threads: int | None = None,
    chunk_snapshots: int = 25_000,
) -> None:
    import duckdb

    workdir = out.parent / f'{out.stem}_work'
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    con = duckdb.connect(str(workdir / 'spike.duckdb'))
    con.execute(f"SET memory_limit='{memory_limit}'")
    con.execute(f"SET temp_directory='{workdir / 'tmp'}'")
    # LP line order is irrelevant to the solver (labels live in the text), so
    # don't pay for insertion-order preservation in COPY.
    con.execute('SET preserve_insertion_order=false')
    if threads:
        con.execute(f'SET threads={threads}')

    gens_pq = str(data_dir / 'generators.parquet')
    loads_pq = str(data_dir / 'load.parquet')

    # mask -> row absence; dense per-dim index for the active set
    con.execute(
        f"""
        CREATE TABLE gens AS
        SELECT generator, p_max, cost,
               ROW_NUMBER() OVER (ORDER BY generator) - 1 AS gidx
        FROM read_parquet('{gens_pq}')
        WHERE p_max > 0
        """
    )
    con.execute(f"CREATE TABLE loads AS SELECT snapshot, load FROM read_parquet('{loads_pq}')")

    # variables: masked coord product, labels via ROW_NUMBER.
    # Partition-wise: a global window materializes all rows (OOMs at ~35M rows
    # under tight limits), so assign labels per snapshot-chunk with a running
    # offset — same result, bounded memory.
    lo, hi = con.execute('SELECT min(snapshot), max(snapshot) FROM loads').fetchone()
    n_active = con.execute('SELECT count(*) FROM gens').fetchone()[0]
    con.execute('CREATE TABLE vars (snapshot BIGINT, gidx BIGINT, var_label BIGINT)')
    offset = 0
    for start in range(lo, hi + 1, chunk_snapshots):
        con.execute(
            f"""
            INSERT INTO vars
            SELECT l.snapshot, g.gidx,
                   {offset} + ROW_NUMBER() OVER (ORDER BY l.snapshot, g.gidx) - 1
            FROM loads l CROSS JOIN gens g
            WHERE l.snapshot >= {start} AND l.snapshot < {start + chunk_snapshots}
            """
        )
        offset += (
            con.execute(
                f'SELECT count(*) FROM loads WHERE snapshot >= {start} AND snapshot < {start + chunk_snapshots}'
            ).fetchone()[0]
            * n_active
        )

    # constraint labels over the foreach set
    con.execute(
        """
        CREATE TABLE cons AS
        SELECT snapshot, load, ROW_NUMBER() OVER (ORDER BY snapshot) - 1 AS con_label
        FROM loads
        """
    )

    obj_part = str(workdir / 'obj.part')
    cons_part = str(workdir / 'cons.part')
    bounds_part = str(workdir / 'bounds.part')

    # objective: sum(p * cost) -> one term row per variable
    con.execute(
        f"""
        COPY (
            SELECT printf('%+.17g x%d', g.cost, v.var_label) AS line
            FROM vars v JOIN gens g USING (gidx)
        ) TO '{obj_part}' {COPY_OPTS}
        """
    )

    # power_balance: sum(p, over=generator) == load -> GROUP BY snapshot.
    # Partition-wise: one COPY per snapshot range, so the string_agg hash
    # aggregate never exceeds chunk_snapshots groups regardless of model size.
    # (An unchunked aggregate or a global sorted-rows rewrite both OOM below
    # ~1GB — duckdb can't spill either shape well at this row count.)
    lo, hi = con.execute('SELECT min(snapshot), max(snapshot) FROM cons').fetchone()
    cons_chunks = []
    for i, start in enumerate(range(lo, hi + 1, chunk_snapshots)):
        chunk_part = f'{cons_part}.{i}'
        cons_chunks.append(chunk_part)
        con.execute(
            f"""
            COPY (
                SELECT printf('c%d:', c.con_label) || chr(10)
                       || string_agg(printf('%+.17g x%d', 1.0, v.var_label), chr(10))
                       || chr(10) || printf('= %.17g', c.load) AS block
                FROM cons c JOIN vars v USING (snapshot)
                WHERE c.snapshot >= {start} AND c.snapshot < {start + chunk_snapshots}
                GROUP BY c.con_label, c.load
            ) TO '{chunk_part}' {COPY_OPTS}
            """
        )

    # bounds: 0 <= p <= p_max
    con.execute(
        f"""
        COPY (
            SELECT printf('0 <= x%d <= %.17g', v.var_label, g.p_max) AS line
            FROM vars v JOIN gens g USING (gidx)
        ) TO '{bounds_part}' {COPY_OPTS}
        """
    )
    con.close()

    with open(out, 'wb') as f:
        f.write(b'min\n\nobj:\n')
        with open(obj_part, 'rb') as part:
            shutil.copyfileobj(part, f)
        f.write(b'\ns.t.\n\n')
        for chunk_part in cons_chunks:
            with open(chunk_part, 'rb') as part:
                shutil.copyfileobj(part, f)
        f.write(b'\nbounds\n')
        with open(bounds_part, 'rb') as part:
            shutil.copyfileobj(part, f)
        f.write(b'\nend\n')

    shutil.rmtree(workdir)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--memory-limit', default='1GB')
    ap.add_argument('--threads', type=int, default=None)
    ap.add_argument('--chunk-snapshots', type=int, default=25_000)
    args = ap.parse_args()

    t0 = time.perf_counter()
    build_and_write(args.data, args.out, args.memory_limit, args.threads, args.chunk_snapshots)
    dt = time.perf_counter() - t0
    print(
        f'duckdb (memory_limit={args.memory_limit}): wrote {args.out} '
        f'({args.out.stat().st_size / 1e6:.1f} MB) in {dt:.2f}s'
    )


if __name__ == '__main__':
    main()
