# Sinks

How a built model leaves the engine — the boxes downstream of the executor in
[docs/ARCHITECTURE.md](../../../../docs/ARCHITECTURE.md)'s pipeline, which
carries the argument for the split. This page is the membership list.

**Two families.** A **solver** takes the tables and runs them; a **writer**
takes the tables and renders them to a file. Everything else follows.

| | solvers/ | writers/ |
|---|---|---|
| answers | `(tables, batch_rows, options) -> (status, objective, primal, dual)` | `(tables, path) -> None` |
| chosen by | **name**, at the call — `solver_name='gurobi'` | **suffix**, from the output — `model.lp` |
| registry | `SOLVERS`, closed | `WRITERS` + `PLANNED_WRITERS`, closed |
| members | `highs.py` (`highspy`, ships), `gurobi.py` (`[gurobi]`: `gurobipy`, `scipy`) | `lp_file.py` (nothing beyond polars) |

`tables.py` is what both read. Neither family imports the other and no member
imports a sibling — `tests/test_architecture.py` reads all of that off the
path, which is what keeps `gurobipy` off the import path of a caller who
solves with HiGHS.

## The contract

A sink takes a `ModelTables` and nothing else: the frames `cols`
(col, lb, ub, vtype), `obj` (col, coeff), `rows` (row, sense, rhs) and `matrix`
(row, col, coeff), plus the counts it chunks by and the objective's sense and
constant — those last two live outside the tables because a constant has no
column to attach to.

A sink never learns how the tables were filled, and the executor never learns
how they are drained. That is the point: adding `mps` is a new module in
`writers/`, not another method on `PolarsExecutor`.

The one thing sinks may share is a *projection* of those frames, never a step
of the work — `ModelTables.dense_columns`, which both solvers read.

## Adding one

**A solver:** `solvers/<name>.py` named for the solver, defining `solve_<name>`
and the `build_<name>` seam `bench/` measures, plus one line in `SOLVERS`.
Import the solver **inside the function** and declare an extra for it — the
module boundary is the fence, the lazy import is what keeps this package free
to import for callers who will never use it. Copy linopy's status map for it
and pin the copy in `tests/test_solve_status.py`, including anywhere you
deliberately diverge.

**A writer:** `writers/<format>.py`, one line in `WRITERS` keyed by suffix,
moved out of `PLANNED_WRITERS` if it was there.

Either way: stream — nothing here may materialise the model a second time —
and nothing above changes. No method on the executor, no branch in `api.py`,
no name on the Python surface.

## When Track 3 lands

[Track 3](../../../../docs/ROADMAP.md#track-3--capabilities-and-the-degree-line)
gives each sink a declared capability table so `check(model, sink=...)` can
answer "will this sink take it". The table belongs in the sink's own module,
collected by the family `__init__` rather than owned by it — and it stops
being uniform at exactly the seam this directory draws: SOS is native in
`gurobi`, a text section in `lp_file`, absent in `highs`.

## Stable output

Two runs of one model produce the same bytes
([#109](https://github.com/FBumann/lpspec/issues/109)). It is not free and it
is easy to lose: a parallel join hands back a group in whatever order it
finished it, so a sink that gathers a row's terms and *then* orders the rows
has already lost the order within one. `lp_file` emits one frame of lines
carrying its own sort key instead, and sorts once. The solvers are ordered for
a different reason — `searchsorted` requires it, and the CSR `indptr` it
produces is only a row's extent if the rows it indexes are sorted.
