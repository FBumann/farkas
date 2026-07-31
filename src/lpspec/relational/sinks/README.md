# Sinks

How a built model leaves the engine. These are the boxes downstream of the
executor in [docs/ARCHITECTURE.md](../../../../docs/ARCHITECTURE.md)'s pipeline.

| Module | Sink | Needs |
|---|---|---|
| `tables.py` | — | the contract every sink reads |
| `lp_file.py` | `lp_file` — LP text | nothing beyond polars |
| `highs.py` | `solver_direct` — COO batches → HiGHS | `highspy` |
| `gurobi.py` | `gurobi` — CSR blocks → gurobipy | `[gurobi]`: `gurobipy`, `scipy` |

## The contract

A sink takes a `ModelTables` and nothing else: the frames `cols`
(col, lb, ub, vtype), `obj` (col, coeff), `rows` (row, sense, rhs) and `matrix`
(row, col, coeff), plus the counts it chunks by and the objective's sense and
constant — those last two live outside the tables because a constant has no
column to attach to.

A sink never learns how the tables were filled, and the executor never learns
how they are drained. That is the point: adding `mps` is a new module here, not
another method on `PolarsExecutor`.

The one thing two sinks may share is a *projection* of those frames, not a
step of the work: `ModelTables.dense_columns` scatters `cols` and `obj` onto
the solver's own column index, and both solver sinks read it. It takes the
solver's spelling of infinity as an argument, because that is the only thing
they disagree about — and it lives on the tables so "both load the same model,
integer for integer" is true by construction rather than by two copies staying
in step.

## Which solver

`SOLVERS` in `__init__.py` maps a name to a solver sink, and `solver(name)`
is the lookup — `ex.solve(solver_name=...)` is the whole of the caller-facing
choice. **The set is closed** and the mapping is a dict literal: nothing
installed can add an entry, because which solver runs is the caller's decision
at the call and never the file's. No YAML key names a solver, and a model
means the same thing whichever one takes it.

Options ride along verbatim in the chosen solver's own vocabulary —
`{'time_limit': 60}` for HiGHS, `{'TimeLimit': 60}` for Gurobi — and a name
the solver rejects surfaces as the solver's own complaint. Translating between
them would mean holding an opinion about every option either solver has.

## Adding a sink

1. New module, named for the sink. Take `ModelTables`, return whatever the sink
   naturally returns (`None` for a writer, `(status, objective)` for a solver).
2. Re-export it from `__init__.py`; a solver sink also goes in `SOLVERS`, whose
   shape is the four-tuple `solve_direct` returns.
3. Thin delegation on `PolarsExecutor` — three lines, no logic.
4. If it needs an optional dependency, import it **inside the function**. The
   module boundary is the fence; the lazy import is what keeps importing this
   package free for callers who will never use that sink.
5. Stream. Nothing here may materialise the model a second time. Sink a lazy
   frame, or hand the solver slices of what the labels already laid out.

## Why one module per sink

Because that is where the fences are. `gurobipy` and `scipy` are optional
dependencies of the `gurobi` sink alone, and a caller solving with HiGHS or
writing LP files should not import them. Splitting by *kind* — all writers
together, all solvers together — would put both optional imports in one module
and a function that branches on which solver you meant. That branch exists, but
it is a dict lookup over modules that never import each other, which is what
keeps the cost of a sink you do not use at zero.

When `mps` lands it may well belong beside `lp_file` (both sink rendered lines
into part files and concatenate them bytewise; they would share `_cat` and the
float formatting). That is a decision to take with
the code in hand, not now — a `text.py` holding one function today would be a
guess about a sink that does not exist.

## When Track 4 lands

[Track 4](../../../../docs/ROADMAP.md#track-4--sink-capabilities) gives each sink a
declared capability table so `check(model, sink=...)` can answer "will this
sink take it". Two notes for whoever writes it:

- The capability table belongs next to the sink it describes, in that sink's
  module. `__init__.py` collects them; it does not own them.
- Make the set of sinks **closed**, like `helpers.BUILTINS` — not an open
  `register_sink()`. An installed plugin that can change the answer to
  `lps.check(model, sink=...)` is hard rule 5's failure mode one level down.
  `SOLVERS` above is that rule already applied to the solvers.
- The `gurobi` sink is where the table stops being uniform: SOS is native
  there, a text section in `lp_file`, and absent in HiGHS. That unevenness is
  the reason the track exists, and it is now in the tree rather than
  hypothetical.

## Stable output

Two runs of one model produce the same bytes
([#109](https://github.com/FBumann/lpspec/issues/109)). It is not free and it
is easy to lose: a parallel join hands back a group in whatever order it
finished it, so a sink that gathers a row's terms and *then* orders the rows
has already lost the order within one. `lp_file` emits one frame of lines
carrying its own sort key instead, and sorts once. `solver_direct` and `gurobi`
are ordered for a different reason — `searchsorted` requires it, and the CSR
`indptr` it produces is only a row's extent if the rows it indexes are sorted.
