"""What a solve returned — the caller's end of the relational lane.

The object ``lps.solve`` hands back, so it is the one piece of this subpackage a
reader meets without going looking. It lives beside the executor rather than in
it because the two answer different questions: the executor *builds* a model,
and this *reads* one — the accessors are joins against the label frames plus
whatever vector the solver left.

Named for linopy's envelope (``Result`` = status + solution + report) rather
than for our own decomposition, because our audience arrives from linopy and a
second vocabulary for one fact is a tax on every one of them
(docs/ARCHITECTURE.md, "Where a concept is already linopy's").
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from lpspec.errors import LpspecError, NoSolutionError

if TYPE_CHECKING:
    import pandas as pd
    import polars as pl

    from lpspec.relational.engines.polars.executor import PolarsExecutor
    from lpspec.relational.status import SolveStatus


@dataclass
class Result:
    """What a solve returned — the outcome, and access to any values.

    Named for linopy's envelope rather than its ``Solution``: it is returned
    when the solve produced *nothing*, so "solution" would be a lie in exactly
    the case a caller most needs to notice.

    **No lifetime to manage.** The model is frames this process owns, so the
    readers stay valid as long as the object does; :meth:`close` releases a
    large one early but nothing breaks without it.

    **Its values are its own.** The result holds the solver's vectors rather
    than reading them back off the executor, so a later solve on the same
    executor cannot rewrite what an earlier result reports. Only the label
    frames — which the coordinates are joined against, and which a re-solve
    does not touch — are shared. Holding them costs nothing until the caller
    retains several results, which is the point: a sweep, a rolling horizon
    and Benders all keep one per iteration.
    """

    _status: SolveStatus
    _objective: float
    _executor: PolarsExecutor
    _primal_values: pl.Series | None = None
    _dual_values: pl.Series | None = None

    @property
    def status(self) -> str:
        """Coarse outcome: ``ok`` / ``warning`` / ``error`` / ``aborted`` / ``unknown``."""
        return self._status.status

    @property
    def termination_condition(self) -> str:
        """What the solver said — ``optimal``, ``infeasible``, ``time_limit``…"""
        return self._status.termination_condition

    @property
    def is_ok(self) -> bool:
        """linopy's rollup: not an error, an abort or a refusal."""
        return self._status.is_ok

    @property
    def has_primal(self) -> bool:
        """Whether there are values to read — what the accessors gate on.

        Narrower than :attr:`is_ok`: a run stopped at a time limit before any
        incumbent is ``ok`` with nothing to read.
        """
        return self._status.is_readable

    @property
    def objective(self) -> float:
        """The objective value, or ``nan`` when there is no solution."""
        return self._objective

    def _require_solution(self, what: str) -> None:
        if self._status.is_readable:
            return
        raise NoSolutionError(
            f'cannot read {what}: the solve terminated {self.termination_condition!r} '
            f'({self._status.solver_wording}), so there are no values to read. Test '
            f'`has_primal` first. This raises rather than returning, because the solver '
            f'hands back a full-length vector of zeros either way and it is '
            f'indistinguishable from an answer.'
        )

    def primal(self, name: str) -> pl.DataFrame:
        """The tidy solution of *name* — ``(dims…, value)``.

        Rows come back in **label order**: row-major over the variable's
        coordinate product, the same order the LP sink writes and the one
        ``var_label`` already encodes. So two reads agree, two runs of one
        model agree, and a solution file can be diffed.
        """
        self._require_solution(f"the primal of '{name}'")
        return self._executor._primal(name, self._primal_values)

    def dual(self, name: str) -> pl.DataFrame:
        """Shadow prices of constraint *name*: ``(dims…, value)``.

        :meth:`primal`'s join against the row frame rather than a column one,
        in that method's order.

        The two empty cases are different failures and both raise rather than
        return zeros: no values at all is
        :class:`~lpspec.errors.NoSolutionError`, while primals without duals —
        any integer variable makes them undefined — raises
        :class:`~lpspec.errors.LpspecError`. Duals exist only on this sink.
        """
        self._require_solution(f"the dual of '{name}'")
        if self._dual_values is None:
            raise LpspecError(self._executor._no_duals_reason(self.termination_condition))
        return self._executor._dual(name, self._dual_values)

    def to_pandas(self, name: str) -> pd.DataFrame:
        """:meth:`primal` as a tidy :class:`pandas.DataFrame`.

        Needs pandas, which ships with the ``[linopy]`` extra. Built column by
        column, since polars' own ``to_pandas`` reaches for pyarrow.
        """
        import pandas as pd

        self._require_solution(f"the primal of '{name}'")
        frame = self._executor._primal(name, self._primal_values)
        return pd.DataFrame({column: frame[column].to_numpy() for column in frame.columns})

    def to_dataarray(self, name: str) -> Any:
        """``primal(name)`` as a labelled :class:`xarray.DataArray`.

        The bridge to array post-processing — ``.sel``, resampling, duration
        curves. Needs the ``[linopy]`` extra; a masked coordinate has no row
        and comes back NaN.
        """
        frame = self.to_pandas(name)
        dims = [c for c in frame.columns if c != 'value']
        if not dims:
            return frame['value'].to_xarray().rename(name)
        return frame.set_index(dims).to_xarray()['value'].rename(name)

    def to_dataset(self, *names: str) -> Any:
        """Variables as one :class:`xarray.Dataset`; all of them by default.

        Costs what it says: each variable arrives dense over its own dims,
        whatever the mask removed, and all of them at once. On a large model,
        name the few you need or use :meth:`to_parquet`.
        """
        assert self._executor._program is not None
        wanted = names or tuple(v.name for v in self._executor._program.variables)
        first, *rest = wanted
        dataset = self.to_dataarray(first).to_dataset(name=first)
        for name in rest:
            dataset[name] = self.to_dataarray(name)
        return dataset

    def to_parquet(self, directory: str | Path) -> dict[str, Path]:
        """One parquet file per variable, ``(dims…, value)``. Returns name → path.

        Sunk straight to disk, never copied into a second representation, in
        :meth:`primal`'s order — so the same model and data write the same
        bytes.
        """
        self._require_solution('the solution')
        return self._executor._solution_to_parquet(Path(directory), self._primal_values)

    def close(self) -> None:
        """Release the built model early. Optional — see the class docstring.

        Drops this result's own values as well as the shared model, so closing
        still frees everything one solve allocated; a sibling result keeps its.
        """
        self._primal_values = self._dual_values = None
        self._executor.close()

    def __enter__(self) -> Result:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        self.close()
        return False
