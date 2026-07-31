# Sinks

How a built model leaves the engine. These are the boxes downstream of the
executor in [docs/ARCHITECTURE.md](../../../../docs/ARCHITECTURE.md)'s pipeline.

**Two families, and that is the whole mental model.** A **solver** takes the
tables and runs them; a **writer** takes the tables and renders them to a file.
Everything else about a sink follows from which of the two it is.

| | solvers/ | writers/ |
|---|---|---|
| takes | `ModelTables` | `ModelTables` |
| returns | `(status, objective, primal, dual)` | nothing; a file exists |
| chosen by | **name**, at the call — `solver_name='gurobi'` | **suffix**, from the output — `model.lp` |
| registry | `SOLVERS`, keyed by solver name | `WRITERS`, keyed by suffix |
| members | `highs.py` (`highspy`, ships), `gurobi.py` (`[gurobi]`: `gurobipy`, `scipy`) | `lp_file.py` (nothing beyond polars) |
| planned | — | `mps`, declared in `PLANNED_WRITERS` |

`tables.py` sits above both and is what they read. Neither family imports the
other, and no member imports a sibling — `tests/test_architecture.py` reads all
of that off the path.

## Why the split is a directory

Because how many solvers there are will change, and what a solver has to answer
will not. A directory that grows one module per member makes the growing side
mechanical: a new solver is a module named for it and a line in `SOLVERS`, with
nothing above it to teach. The same reasoning made `engines/` a directory —
*the engine is a directory, not a convention*.

It also puts the fence where the optional dependencies are. `gurobipy` and
`scipy` belong to `solvers/gurobi.py` alone, and a caller solving with HiGHS or
writing LP files must not import them; one module per member plus "no member
reaches a sibling" is what makes that true rather than intended.

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
of the work: `ModelTables.dense_columns` scatters `cols` and `obj` onto the
solver's own column index, and both solvers read it. It takes the solver's
spelling of infinity as an argument, because that is the only thing they
disagree about — and it lives on the tables so "both load the same model,
integer for integer" is true by construction rather than by two copies staying
in step.

## Both registries are closed

`SOLVERS` and `WRITERS` are dict literals, not something an installed package
can add to — `helpers.BUILTINS`' rule one level down. For solvers that matters
most: which solver runs is the **caller's** choice at the call and never the
file's. No YAML key names a solver, a model means the same thing whichever one
takes it, and an installed plugin that could change what `solver_name='x'`
resolves to is hard rule 5's failure mode in miniature.

Solver options ride along verbatim in the chosen solver's own vocabulary —
`{'time_limit': 60}` for HiGHS, `{'TimeLimit': 60}` for Gurobi — and a name the
solver rejects surfaces as the solver's own complaint. Translating between them
would mean holding an opinion about every option either solver has.

## Adding one

**A solver:**

1. `solvers/<name>.py`, named for the solver. Define `solve_<name>` with the
   family's shape and `build_<name>`, the load-only seam `bench/` measures.
2. One line in `SOLVERS`.
3. Import the solver **inside the function**, and declare an extra for it. The
   module boundary is the fence; the lazy import is what keeps importing this
   package free for callers who will never use that solver.
4. Copy linopy's status map for it, and pin the copy against linopy in
   `tests/test_solve_status.py` — including anywhere you deliberately diverge,
   which is declared rather than silent.
5. Stream. Hand the solver slices of what the labels already laid out; nothing
   here may materialise the model a second time.

**A writer:** `writers/<format>.py`, one line in `WRITERS` keyed by suffix,
moved out of `PLANNED_WRITERS` if it was there. Sink a lazy frame; see *stable
output* below.

Nothing else changes in either case — no method on the executor, no branch in
`api.py`, no name added to the Python surface.

## When Track 3 lands

[Track 3](../../../../docs/ROADMAP.md#track-3--capabilities-and-the-degree-line)
gives each sink a declared capability table so `check(model, sink=...)` can
answer "will this sink take it". Two notes for whoever writes it:

- The capability table belongs next to the sink it describes, in that sink's
  module. The family `__init__` collects them; it does not own them.
- The table stops being uniform at exactly the seam this directory already
  draws: SOS is native in `gurobi`, a text section in `lp_file`, and absent in
  `highs`. That unevenness is the reason the track exists, and it is now in the
  tree rather than hypothetical.

## Stable output

Two runs of one model produce the same bytes
([#109](https://github.com/FBumann/lpspec/issues/109)). It is not free and it
is easy to lose: a parallel join hands back a group in whatever order it
finished it, so a sink that gathers a row's terms and *then* orders the rows
has already lost the order within one. `lp_file` emits one frame of lines
carrying its own sort key instead, and sorts once. The solvers are ordered for
a different reason — `searchsorted` requires it, and the CSR `indptr` it
produces is only a row's extent if the rows it indexes are sorted.
