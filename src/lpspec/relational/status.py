"""What a solve returned, on two axes.

Copied — deliberately, spelling for spelling — from ``linopy.constants``.
Anyone arriving from linopy should not have to learn a second vocabulary for
the same facts, and `tests/test_solve_status.py` asserts the tables still
match linopy's, so drift is a test failure rather than a discovery.

Nothing here imports linopy: the engine may not (docs/ARCHITECTURE.md, hard rule
2). The test may, and does — linopy is the oracle for this the same way it is
for the math.

The two axes are worth keeping separate. ``termination_condition`` is what
the solver said; ``status`` is what it means for the caller. Note that **ok
does not mean optimal** — a run stopped at a time limit with an incumbent is
``ok``, because there are values worth reading. That is precisely the
question a caller has, so it is the one :attr:`SolveStatus.is_ok` answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SolverStatusName = Literal['ok', 'warning', 'error', 'aborted', 'unknown']

TerminationConditionName = Literal[
    'unknown',
    'optimal',
    'time_limit',
    'iteration_limit',
    'terminated_by_limit',
    'suboptimal',
    'imprecise',
    'unbounded',
    'infeasible',
    'infeasible_or_unbounded',
    'other',
    'internal_solver_error',
    'error',
    'user_interrupt',
    'resource_interrupt',
    'licensing_problems',
]

#: Which termination conditions roll up to which coarse status.
STATUS_TO_TERMINATION_CONDITIONS: dict[str, frozenset[str]] = {
    'ok': frozenset({'optimal', 'time_limit', 'iteration_limit', 'terminated_by_limit', 'suboptimal', 'imprecise'}),
    'warning': frozenset({'infeasible', 'infeasible_or_unbounded', 'unbounded', 'other'}),
    'error': frozenset({'internal_solver_error', 'error'}),
    'aborted': frozenset({'user_interrupt', 'resource_interrupt', 'licensing_problems'}),
    'unknown': frozenset({'unknown'}),
}


def status_of(termination_condition: str) -> str:
    """The coarse status a termination condition rolls up to."""
    for status, conditions in STATUS_TO_TERMINATION_CONDITIONS.items():
        if termination_condition in conditions:
            return status
    return 'unknown'


@dataclass(frozen=True)
class SolveStatus:
    """The outcome of a solve, on both axes plus the solver's own wording."""

    termination_condition: str
    #: Exactly what the solver called it, for a message a user can search for.
    solver_wording: str = ''
    #: Whether the solver reports an actual primal, which the termination
    #: condition does not tell you — see :attr:`is_readable`.
    has_primal: bool = True

    @property
    def status(self) -> str:
        return status_of(self.termination_condition)

    @property
    def is_ok(self) -> bool:
        """linopy's rollup: the run is not an error, an abort or a refusal.

        Kept exactly as linopy defines it, because it is shared vocabulary.
        It is *not* the question "can I read values" — see
        :attr:`is_readable`.
        """
        return self.status == 'ok'

    @property
    def is_readable(self) -> bool:
        """Whether there are primal values to read.

        `is_ok` alone is not enough, and this is where we deliberately go
        beyond linopy. Its `safe_get_solution` gates on `is_ok`, so a MIP
        stopped at a time limit **before finding any incumbent** is `ok` and
        its zero-filled `col_value` is read as though it were an answer. That
        is the bug this package just fixed one level down (#115), so
        inheriting it would be a poor trade for vocabulary parity.

        `optimal` always has a primal. Every other `ok` condition —
        `time_limit`, `iteration_limit`, `terminated_by_limit`, `suboptimal`,
        `imprecise` — means "stopped early", and whether an incumbent exists
        is a separate fact only the solver knows.
        """
        return self.is_ok and self.has_primal
