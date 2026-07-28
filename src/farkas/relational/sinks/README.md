# Sinks

How a built model leaves the engine. These are the two boxes downstream of the
executor in [ARCHITECTURE.md](../../../ARCHITECTURE.md)'s pipeline.

| Module | Sink | Needs |
|---|---|---|
| `tables.py` | — | the contract every sink reads |
| `lp_file.py` | `lp_file` — LP text | nothing beyond polars |
| `highs.py` | `solver_direct` — COO batches → HiGHS | `highspy` |

## The contract

A sink takes a `ModelTables` and nothing else: the frames `cols`
(col, lb, ub, vtype), `obj` (col, coeff), `rows` (row, sense, rhs) and `matrix`
(row, col, coeff), plus the counts it chunks by and the objective's sense and
constant — those last two live outside the tables because a constant has no
column to attach to.

A sink never learns how the tables were filled, and the executor never learns
how they are drained. That is the point: adding `mps` is a new module here, not
another method on `PolarsExecutor`.

## Adding a sink

1. New module, named for the sink. Take `ModelTables`, return whatever the sink
   naturally returns (`None` for a writer, `(status, objective)` for a solver).
2. Re-export it from `__init__.py`.
3. Thin delegation on `PolarsExecutor` — three lines, no logic.
4. If it needs an optional dependency, import it **inside the function**. The
   module boundary is the fence; the lazy import is what keeps importing this
   package free for callers who will never use that sink.
5. Stream. Nothing here may materialise the model a second time. Sink a lazy
   frame, or hand the solver slices of what the labels already laid out.

## Why one module per sink

Because that is where the fences are. `highspy` is an optional dependency of
`solver_direct` alone, and a caller that only writes LP files should not import
it. Splitting by *kind* — all writers together, all solvers together — would
put two optional imports in one module and a function that branches on which
solver you meant.

When `mps` lands it may well belong beside `lp_file` (both sink rendered lines
into part files and concatenate them bytewise; they would share `_cat` and the
float formatting). That is a decision to take with
the code in hand, not now — a `text.py` holding one function today would be a
guess about a sink that does not exist.

## When Track 4 lands

[Track 4](../../../ROADMAP.md#track-4--sink-capabilities) gives each sink a
declared capability table so `check(model, sink=...)` can answer "will this
sink take it". Two notes for whoever writes it:

- The capability table belongs next to the sink it describes, in that sink's
  module. `__init__.py` collects them; it does not own them.
- Make the set of sinks **closed**, like `helpers.BUILTINS` — not an open
  `register_sink()`. An installed plugin that can change the answer to
  `fk.check(model, sink=...)` is hard rule 5's failure mode one level down.

## Stable output

Two runs of one model produce the same bytes
([#109](https://github.com/FBumann/farkas/issues/109)). It is not free and it
is easy to lose: a parallel join hands back a group in whatever order it
finished it, so a sink that gathers a row's terms and *then* orders the rows
has already lost the order within one. `lp_file` emits one frame of lines
carrying its own sort key instead, and sorts once. `solver_direct` is ordered
for a different reason — `searchsorted` requires it.
