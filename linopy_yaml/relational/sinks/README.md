# Sinks

How a built model leaves the engine. These are the two boxes downstream of the
executor in [ARCHITECTURE.md](../../../ARCHITECTURE.md)'s pipeline.

| Module | Sink | Needs |
|---|---|---|
| `tables.py` | — | the contract every sink reads |
| `lp_file.py` | `lp_file` — LP text | nothing beyond duckdb |
| `highs.py` | `solver_direct` — COO batches → HiGHS | `highspy` |

## The contract

A sink takes a `ModelTables` and nothing else: a connection holding `cols`
(col, lb, ub, vtype), `obj` (col, coeff), `rows` (row, sense, rhs) and `A`
(row, col, coeff), plus the counts it chunks by and the objective's sense and
constant — those last two live outside the tables because a constant has no
column to attach to.

A sink never learns how the tables were filled, and the executor never learns
how they are drained. That is the point: adding `mps` is a new module here, not
another method on `DuckdbExecutor`.

## Adding a sink

1. New module, named for the sink. Take `ModelTables`, return whatever the sink
   naturally returns (`None` for a writer, `(status, objective)` for a solver).
2. Re-export it from `__init__.py`.
3. Thin delegation on `DuckdbExecutor` — three lines, no logic.
4. If it needs an optional dependency, import it **inside the function**. The
   module boundary is the fence; the lazy import is what keeps importing this
   package free for callers who will never use that sink.
5. Stream. Nothing here may materialise the model — hard rule 4. Aggregate
   inside duckdb, or hand the solver batches.

## Why one module per sink

Because that is where the fences are. `highspy` is an optional dependency of
`solver_direct` alone, and a caller that only writes LP files should not import
it. Splitting by *kind* — all writers together, all solvers together — would
put two optional imports in one module and a function that branches on which
solver you meant.

When `mps` lands it may well belong beside `lp_file` (both are chunked `COPY`
of `printf`'d rows into part files, concatenated bytewise; they would share
`_cat`, the chunking and the float formatting). That is a decision to take with
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
  `ly.check(model, sink=...)` is hard rule 5's failure mode one level down.

## Known issue

Neither file sink emits in a stable order: several `COPY` statements have no
`ORDER BY` and `preserve_insertion_order=false` is set on the connection, so
two runs of the same model produce byte-different files with identical content
— [#109](https://github.com/FBumann/linopy-yaml/issues/109). `solver_direct` is
unaffected; every read it makes is explicitly ordered, because `searchsorted`
requires it.
