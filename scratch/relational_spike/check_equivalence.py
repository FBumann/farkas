"""Differential check: solve both LP files with HiGHS and compare.

The eager linopy build is the correctness oracle. Labels and file layout may
differ; the models are equivalent iff dimensions match and objectives agree.
"""

import argparse
import sys
from pathlib import Path


def solve(path: Path) -> tuple[str, float, int, int]:
    import highspy

    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    status = h.readModel(str(path))
    if status != highspy.HighsStatus.kOk:
        raise RuntimeError(f"HiGHS could not read {path}")
    lp = h.getLp()
    n_cols, n_rows = lp.num_col_, lp.num_row_
    h.run()
    model_status = h.getModelStatus()
    obj = h.getInfo().objective_function_value
    return str(model_status), obj, n_cols, n_rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("lp_a", type=Path)
    ap.add_argument("lp_b", type=Path)
    ap.add_argument("--rtol", type=float, default=1e-8)
    args = ap.parse_args()

    status_a, obj_a, cols_a, rows_a = solve(args.lp_a)
    status_b, obj_b, cols_b, rows_b = solve(args.lp_b)

    print(f"{args.lp_a}: {status_a}, obj={obj_a!r}, vars={cols_a:,}, cons={rows_a:,}")
    print(f"{args.lp_b}: {status_b}, obj={obj_b!r}, vars={cols_b:,}, cons={rows_b:,}")

    ok = (
        status_a == status_b
        and cols_a == cols_b
        and rows_a == rows_b
        and abs(obj_a - obj_b) <= args.rtol * max(1.0, abs(obj_a))
    )
    if ok:
        print("EQUIVALENT")
    else:
        print("MISMATCH")
        sys.exit(1)


if __name__ == "__main__":
    main()
