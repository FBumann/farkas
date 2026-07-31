"""What an engine is, and everything an engine does not have to write.

`plan.py` is what an engine consumes and `sinks/tables.py` is what it produces.
This is the third side: given those two, most of an executor's surface is not
engine work at all. Sinking to an LP file, handing the model to HiGHS, and
joining a solver's answer back onto coordinates are all written against
`ModelTables` and the label frames — never against how either was filled.

So they live here once. An engine supplies four things:

- `build(program, sources)` — bind and construct
- `_tables()` — the four frames plus the scalars
- `_variables` / `_constraints` — `(dims…, var_label)` and `(dims…, row)` per
  declaration, which is what a solution is read back through
- `_program` — the plan it built, for the dims a read-back projects to

and inherits the rest. That split is the actual claim `engines/` makes, and it
is why a second engine is a compiler and an assembler rather than a whole lane.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal

from lpspec.relational import sinks
from lpspec.relational.result import Result

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import polars as pl

    from lpspec.relational import plan


class Engine(ABC):
    """A relational LP builder: plan in, `ModelTables` out.

    The label registries are declared here rather than in each engine because
    the read-back below is written against them. They are polars frames on both
    engines: a label frame is `(dims…, label)` and nothing about it is engine
    work — an engine that holds its labels elsewhere materialises them here,
    which is the price of not writing this file twice.
    """

    _program: plan.Program | None

    @property
    @abstractmethod
    def _variables(self) -> Mapping[str, pl.LazyFrame]:
        """Per-variable `(dims…, var_label)`. Read-only here; an engine owns the storage."""

    @property
    @abstractmethod
    def _constraints(self) -> Mapping[str, pl.LazyFrame]:
        """Per-constraint `(dims…, row)`. Read-only here; an engine owns the storage."""

    @abstractmethod
    def build(self, program: plan.Program, sources: Mapping[str, Any]) -> None:
        """Bind *sources* and build every declaration. Raises rather than half-building."""

    @abstractmethod
    def _tables(self) -> sinks.ModelTables:
        """The built model as `cols`, `obj`, `rows`, `matrix` plus its scalars."""

    @abstractmethod
    def close(self) -> None:
        """Drop the built model. Optional for a caller — see `Result`."""

    # -- sinks: written against ModelTables, so neither engine owns them ---

    def write_lp(self, path: str | Path) -> None:
        """Sink the built model to an LP file."""
        sinks.write_lp_file(self._tables(), path)

    def solve(
        self,
        batch_rows: int | None = None,
        solver_options: Mapping[str, Any] | None = None,
    ) -> Result:
        """Sink the built model straight into HiGHS and solve it.

        ``solver_options`` is forwarded verbatim to the solver, the way
        linopy's is — ``{'time_limit': 60, 'mip_rel_gap': 0.01}``.
        ``batch_rows`` is the hand-off budget in elements, and defaults to the
        sink's own — see :data:`~lpspec.relational.sinks.highs.HANDOFF_BUDGET`.
        """
        status, objective, primal, dual = sinks.solve_direct(self._tables(), batch_rows, solver_options)
        return Result(
            _status=status,
            _objective=objective,
            _executor=self,
            _primal_values=primal,
            _dual_values=dual,
        )

    # -- read-back: a label join, and labels are frames on every engine ----

    def _solution_frame(self, name: str, values: pl.DataFrame | None) -> pl.LazyFrame:
        """The tidy solution of variable *name*: ``(dims…, value)``.

        A label join, never a dense array. *values* is the solver's column
        vector, held by the :class:`Result` that asks — the labels are the
        build's and shared, the values are one solve's and are not.

        **Ordered by label**, which is the order the coordinates already have:
        a label *is* row-major position in the coordinate product, so sorting
        on it hands the caller back the model's own order rather than the
        order a hash join happened to finish in. Stated rather than inherited,
        because neither input is guaranteed sorted — a mask decides which rows
        of the product survive, not how they arrive.
        """
        assert self._program is not None
        assert values is not None, 'no solve has stored a primal'
        dims = self._program.variable(name).dims
        return (
            self._variables[name]
            .join(values.lazy(), left_on='var_label', right_on='col', how='inner')
            .sort('var_label')
            .select(*dims, 'value')
        )

    def _primal(self, name: str, values: pl.DataFrame | None) -> pl.DataFrame:
        return self._solution_frame(name, values).collect(engine='streaming')

    def _dual(self, name: str, values: pl.DataFrame) -> pl.DataFrame:
        """:meth:`_solution_frame` against row labels instead of column ones.

        Ordered the same way, for the same reason — a constraint row's label
        is its position in that constraint's coordinate product.
        """
        assert self._program is not None
        dims = self._program.constraint(name).dims
        return (
            self._constraints[name]
            .join(values.lazy(), on='row', how='inner')
            .sort('row')
            .select(*dims, 'value')
            .collect(engine='streaming')
        )

    def _no_duals_reason(self, termination_condition: str) -> str:
        """Why a solve that *did* leave values still has no duals.

        Integrality is decidable from the program, and naming the variable is
        actionable where "the solver reported none" is not.
        """
        assert self._program is not None
        discrete = sorted(v.name for v in self._program.variables if v.variable_type != 'continuous')
        if discrete:
            names = ', '.join(f"'{n}'" for n in discrete)
            return (
                f'duals are undefined for a mixed-integer model: {names} '
                f'{"is" if len(discrete) == 1 else "are"} not continuous. '
                f'Drop the integrality to price the LP relaxation instead.'
            )
        return (
            f'the solver returned no dual solution, though the solve terminated '
            f'{termination_condition!r}. Duals come from a simplex basis, which a '
            f'run stopped short of one does not have.'
        )

    def _solution_to_parquet(self, directory: Path, values: pl.DataFrame | None) -> dict[str, Path]:
        assert self._program is not None
        directory.mkdir(parents=True, exist_ok=True)
        written: dict[str, Path] = {}
        for v in self._program.variables:
            out = directory / f'{v.name}.parquet'
            self._solution_frame(v.name, values).sink_parquet(out)
            written[v.name] = out
        return written

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        self.close()
        return False
