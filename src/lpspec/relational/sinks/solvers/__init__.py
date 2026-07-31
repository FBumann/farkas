"""The solver family: the tables in, an answer out. See ../README.md.

One module per solver, **named for the solver** — nothing here is named for
the mechanism, because every member uses the same one. Each answers::

    (tables, batch_rows, solver_options) -> (status, objective, primal, dual)

plus a ``build_<solver>`` that loads the model and stops, which is the seam
`bench/` measures. ``tests/test_architecture.py`` checks that off the path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lpspec.errors import LpspecError
from lpspec.relational.sinks.solvers.gurobi import solve_gurobi
from lpspec.relational.sinks.solvers.highs import solve_highs

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import Any

    import polars as pl

    from lpspec.relational.sinks.tables import ModelTables
    from lpspec.relational.status import SolveStatus

    Solve = Callable[
        [ModelTables, int | None, Mapping[str, Any] | None],
        tuple[SolveStatus, float, pl.Series | None, pl.Series | None],
    ]

__all__ = ['SOLVERS', 'solver']

#: Every solver a caller may name, and **closed** — a dict literal, not a
#: registry something installed can add to. Which solver runs is the caller's
#: choice at the call and never the file's, so an installed package that could
#: change what ``solver_name='x'`` resolves to is hard rule 5 one level down.
SOLVERS: Mapping[str, Solve] = {
    'highs': solve_highs,
    'gurobi': solve_gurobi,
}


def solver(name: str) -> Solve:
    """The solver called *name*, or an error listing every alternative."""
    try:
        return SOLVERS[name]
    except KeyError:
        raise LpspecError(
            f'unknown solver {name!r} — this build solves with {", ".join(sorted(SOLVERS))}. '
            'HiGHS ships with the package and is the default; gurobi needs the [gurobi] extra.'
        ) from None
