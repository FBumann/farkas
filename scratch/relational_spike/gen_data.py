"""Generate parquet inputs for the dispatch benchmark model.

Same math as examples/dispatch.yaml, scaled by --snapshots/--generators:

    p[snapshot, generator]        where p_max > 0, bounds 0..p_max
    power_balance[snapshot]:      sum(p, over=generator) == load
    min sum(p * cost)
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", type=int, default=1000)
    ap.add_argument("--generators", type=int, default=10)
    ap.add_argument(
        "--masked-frac",
        type=float,
        default=0.1,
        help="fraction of generators with p_max == 0",
    )
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    n_gen = args.generators

    p_max = rng.uniform(50.0, 200.0, n_gen)
    p_max[rng.random(n_gen) < args.masked_frac] = 0.0
    cost = rng.uniform(5.0, 100.0, n_gen).round(4)
    generators = pd.DataFrame(
        {
            "generator": [f"g{i:05d}" for i in range(n_gen)],
            "p_max": p_max.round(4),
            "cost": cost,
        }
    )

    # keep the problem feasible: load stays below total active capacity
    capacity = generators["p_max"].sum()
    load = (rng.uniform(0.2, 0.8, args.snapshots) * capacity).round(4)
    loads = pd.DataFrame(
        {"snapshot": np.arange(args.snapshots, dtype=np.int64), "load": load}
    )

    args.out.mkdir(parents=True, exist_ok=True)
    generators.to_parquet(args.out / "generators.parquet", index=False)
    loads.to_parquet(args.out / "load.parquet", index=False)

    n_masked = int((generators["p_max"] == 0).sum())
    n_vars = (n_gen - n_masked) * args.snapshots
    print(
        f"wrote {n_gen} generators ({n_masked} masked), {args.snapshots} snapshots "
        f"-> {n_vars:,} variables, {args.snapshots:,} constraints ({args.out})"
    )


if __name__ == "__main__":
    main()
