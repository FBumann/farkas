"""The solver family: the tables in, an answer out.

One module per solver, **named for the solver** — the way ``engines/`` is
named for its engine. Nothing here is named for the mechanism, because every
member uses the same one: the tables go over as arrays, never as text.

Every member answers one shape::

    (tables, batch_rows, solver_options) -> (status, objective, primal, dual)

plus a ``build_<solver>`` that loads the model and stops, which is the seam
`bench/` measures — the search is the same work whoever filled the model.
``tests/test_architecture.py`` checks the shape off the path, so a module in
this directory cannot quietly answer a different one.

A member imports its own solver lazily and no sibling ever: the module
boundary is the fence that keeps ``gurobipy`` off the import path of a caller
who solves with HiGHS.
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
        tuple[SolveStatus, float, pl.DataFrame | None, pl.DataFrame | None],
    ]

__all__ = ['SOLVERS', 'solver']

#: Every solver a caller may name, and **the set is closed** — a dict literal,
#: not a registry something installed can add to. Which solver runs is the
#: caller's choice at the call and never the file's: no YAML key names one, and
#: a model means the same thing whichever takes it. An installed package that
#: could change what ``solver_name='x'`` resolves to is hard rule 5's failure
#: mode one level down.
#:
#: Names are the solvers' own, lowercased. How many there are will change; what
#: a member has to answer will not.
SOLVERS: Mapping[str, Solve] = {
    'highs': solve_highs,
    'gurobi': solve_gurobi,
}


def solver(name: str) -> Solve:
    """The solver called *name*.

    The lookup lives with the family so the closed set and the message for a
    name outside it stay next to what they are about. The message names every
    alternative, because the set is small and knowing it is the answer to the
    question being asked.
    """
    try:
        return SOLVERS[name]
    except KeyError:
        raise LpspecError(
            f'unknown solver {name!r} — this build solves with {", ".join(sorted(SOLVERS))}. '
            'HiGHS ships with the package and is the default; gurobi needs the [gurobi] extra.'
        ) from None
