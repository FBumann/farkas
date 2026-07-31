"""Sinks: how a built model leaves the engine.

docs/ARCHITECTURE.md's pipeline draws the boxes downstream of the executor.
This package is those boxes — one module each, because that is where the
fences are: ``gurobipy`` is an optional dependency of the ``gurobi`` sink
alone, and a caller that solves with HiGHS or only writes LP files should
never import it.

A sink reads :class:`ModelTables` and nothing else. No sink knows how the
tables were filled, and the executor does not know how they are drained, which
is what makes the planned ``mps`` sink a new module here rather than another
method on the executor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lpspec.errors import LpspecError
from lpspec.relational.sinks.gurobi import build_gurobi, solve_gurobi
from lpspec.relational.sinks.highs import build_highs, solve_direct
from lpspec.relational.sinks.lp_file import write_lp_file
from lpspec.relational.sinks.tables import ModelTables

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import Any

    import polars as pl

    from lpspec.relational.status import SolveStatus

__all__ = [
    'SOLVERS',
    'ModelTables',
    'build_gurobi',
    'build_highs',
    'solve_direct',
    'solve_gurobi',
    'solver',
    'write_lp_file',
]

#: The solver sinks a caller may name, and the whole of them. **Closed on
#: purpose** (README, "Adding a sink"): an installed package that could add an
#: entry here is hard rule 5's failure mode one level down, since it would
#: change what ``solver_name`` means for a file that never mentions one. Which
#: solver a build goes to is the caller's choice at the call, never the
#: model's — a YAML file cannot express it and does not know.
SOLVERS: Mapping[
    str,
    Callable[
        [ModelTables, int | None, Mapping[str, Any] | None],
        tuple[SolveStatus, float, pl.DataFrame | None, pl.DataFrame | None],
    ],
] = {
    'highs': solve_direct,
    'gurobi': solve_gurobi,
}


def solver(name: str) -> Callable[..., tuple[SolveStatus, float, pl.DataFrame | None, pl.DataFrame | None]]:
    """The solver sink called *name*.

    Here rather than in the executor so the closed set and the message for a
    name outside it stay next to the sinks they are about. The message names
    every alternative, because the set is small and knowing it is the answer
    to the question being asked.
    """
    try:
        return SOLVERS[name]
    except KeyError:
        raise LpspecError(
            f'unknown solver {name!r} — the sinks that solve are {", ".join(sorted(SOLVERS))}. '
            'HiGHS ships with the package and is the default; gurobi needs the [gurobi] extra.'
        ) from None
