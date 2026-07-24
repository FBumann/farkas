"""Eager linopy baseline: build the dispatch model from parquet and write an LP file.

This mirrors what linopy_yaml's eager builder does with examples/dispatch.yaml:
dense xarray arrays, mask via `mask=`, LP output via linopy's writer.
"""

import argparse
import time
from pathlib import Path

import pandas as pd
import xarray as xr


def build_and_write(data_dir: Path, out: Path, io_api: str = "lp-polars") -> None:
    import linopy

    generators = pd.read_parquet(data_dir / "generators.parquet").set_index("generator")
    loads = pd.read_parquet(data_dir / "load.parquet").set_index("snapshot")

    p_max = xr.DataArray.from_series(generators["p_max"])
    cost = xr.DataArray.from_series(generators["cost"])
    load = xr.DataArray.from_series(loads["load"])

    m = linopy.Model()
    mask = (p_max > 0).broadcast_like(load * p_max)
    p = m.add_variables(
        lower=0,
        upper=p_max,
        coords=[loads.index, generators.index],
        name="p",
        mask=mask,
    )
    m.add_constraints(p.sum("generator") == load, name="power_balance")
    m.add_objective((p * cost).sum())
    m.to_file(out, io_api=io_api, progress=False)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--io-api", default="lp-polars", choices=["lp", "lp-polars"])
    args = ap.parse_args()

    t0 = time.perf_counter()
    build_and_write(args.data, args.out, args.io_api)
    dt = time.perf_counter() - t0
    print(
        f"linopy ({args.io_api}): wrote {args.out} ({args.out.stat().st_size / 1e6:.1f} MB) in {dt:.2f}s"
    )


if __name__ == "__main__":
    main()
