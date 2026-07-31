"""The ``gurobi`` sink, against the sink that was already here.

Two sinks loading one :class:`ModelTables` must produce the same model, so
HiGHS is the oracle for Gurobi the way linopy is the oracle for the math: the
interesting assertions here are agreements, not values. Where a value *is*
asserted it comes from ``examples/ports/references.json`` — somebody else's
published optimum, which neither sink can talk the other into.

Every test skips without ``gurobipy``. It ships a size-limited licence in its
own wheel, which is what makes this runnable in CI at all, so the models here
stay small enough for it — a few hundred columns, where the limit is 2000.
"""

from __future__ import annotations

import builtins
from typing import Any

import polars as pl
import pytest

import lpspec as lps
from lpspec.errors import LpspecError, NoSolutionError
from lpspec.relational.sinks.solvers.gurobi import build_gurobi
from tests.test_ports import PORTS, REFERENCES, sources

gurobipy = pytest.importorskip('gurobipy', reason='the gurobi sink needs the [gurobi] extra')


LP = {
    'dimensions': {'t': {'dtype': 'int', 'values': [0, 1, 2]}},
    'parameters': {'load': {'dims': ['t']}, 'price': {'dims': ['t']}},
    'variables': {'p': {'foreach': ['t'], 'bounds': {'lower': 0, 'upper': 100}}},
    'constraints': {'meet': {'foreach': ['t'], 'expression': 'p >= load'}},
    'objectives': {'cost': {'sense': 'minimize', 'expression': 'sum(p * price, over=t)'}},
}

#: Maximisation *and* an objective constant, which are the two things the
#: sink states outside the frames: ``ModelSense`` and ``ObjCon``.
MAX = {
    'dimensions': {'t': {'dtype': 'int', 'values': [0, 1]}},
    'parameters': {'cap': {'dims': ['t']}},
    'variables': {'p': {'foreach': ['t'], 'bounds': {'lower': 0, 'upper': 10}}},
    'constraints': {'lim': {'foreach': ['t'], 'expression': 'p <= cap'}},
    'objectives': {'profit': {'sense': 'maximize', 'expression': 'sum(p, over=t) + 5'}},
}

MIP = {
    'dimensions': {'i': {'dtype': 'int', 'values': [0, 1, 2]}, 'one': {'dtype': 'int', 'values': [0]}},
    'parameters': {'w': {'dims': ['i']}, 'cap': {'dims': ['one']}},
    'variables': {'x': {'foreach': ['i'], 'binary': True}},
    'constraints': {'budget': {'foreach': ['one'], 'expression': 'sum(x * w, over=i) <= cap'}},
    'objectives': {'o': {'sense': 'maximize', 'expression': 'sum(x * w, over=i)'}},
}

INFEASIBLE = {
    'dimensions': {'t': {'dtype': 'int', 'values': [0]}},
    'parameters': {'load': {'dims': ['t']}},
    'variables': {'p': {'foreach': ['t'], 'bounds': {'lower': 0, 'upper': 1}}},
    'constraints': {'meet': {'foreach': ['t'], 'expression': 'p == load'}},
    'objectives': {'c': {'sense': 'minimize', 'expression': 'p'}},
}

DATA: dict[str, dict[str, Any]] = {
    'LP': {
        'load': pl.DataFrame({'t': [0, 1, 2], 'value': [1.0, 2.0, 3.0]}),
        'price': pl.DataFrame({'t': [0, 1, 2], 'value': [10.0, 20.0, 30.0]}),
    },
    'MAX': {'cap': pl.DataFrame({'t': [0, 1], 'value': [3.0, 4.0]})},
    'MIP': {
        'w': pl.DataFrame({'i': [0, 1, 2], 'value': [2.0, 3.0, 4.0]}),
        'cap': pl.DataFrame({'one': [0], 'value': [5.0]}),
    },
    'INFEASIBLE': {'load': pl.DataFrame({'t': [0], 'value': [99.0]})},
}

MODELS = {'LP': LP, 'MAX': MAX, 'MIP': MIP}


# ---------------------------------------------------------------------------
# the two sinks answer the same question the same way
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('name', 'variable', 'constraint'), [('LP', 'p', 'meet'), ('MAX', 'p', 'lim'), ('MIP', 'x', None)]
)
def test_gurobi_and_highs_agree(name: str, variable: str, constraint: str | None) -> None:
    """The claim the second solver has to earn, on all three quantities.

    Coordinates as well as values, since a sink that loaded the columns in a
    different order would still reach the same objective on these models — and
    duals under ``maximize``, where a sign convention could differ and nothing
    else in the suite would notice.
    """
    with lps.solve(MODELS[name], DATA[name]) as highs, lps.solve(MODELS[name], DATA[name], solver_name='gurobi') as gb:
        assert gb.termination_condition == highs.termination_condition
        assert gb.objective == pytest.approx(highs.objective)

        expected, got = highs.primal(variable), gb.primal(variable)
        assert got.columns == expected.columns
        assert got.drop('value').equals(expected.drop('value'))
        assert got['value'].to_list() == pytest.approx(expected['value'].to_list())

        if constraint:
            assert gb.dual(constraint)['value'].to_list() == pytest.approx(highs.dual(constraint)['value'].to_list())


@pytest.mark.parametrize('port', sorted(REFERENCES), ids=str)
def test_every_port_reaches_its_reference_optimum_on_gurobi(port: str) -> None:
    """``test_ports.py``'s corpus, solved by the other solver.

    The one assertion here no part of this package produced. A sink that
    mis-loads the matrix — a block boundary off by a row, a sense inverted —
    still reaches *a* number; this is what that number is checked against.
    """
    reference = REFERENCES[port]
    with lps.solve(PORTS / f'{port}.yaml', sources(port), solver_name='gurobi') as solution:
        assert solution.is_ok, f'{port} did not solve: {solution.status}'
        assert solution.objective == pytest.approx(reference['objective'], rel=reference['rtol'])


def test_block_boundaries_do_not_move_the_answer() -> None:
    """``batch_rows=1`` forces one block per row, so every CSR view is built at
    a boundary — where an off-by-one in ``indptr`` shifts coefficients into the
    neighbouring row rather than dropping them."""
    with lps.build(LP, DATA['LP']) as ex:
        whole = ex.solve(solver_name='gurobi')
        ragged = ex.solve(batch_rows=1, solver_name='gurobi')
        assert ragged.objective == pytest.approx(whole.objective)
        assert ragged.primal('p')['value'].to_list() == pytest.approx(whole.primal('p')['value'].to_list())


# ---------------------------------------------------------------------------
# what the sink says when there is nothing to read
# ---------------------------------------------------------------------------


def test_an_infeasible_solve_reports_both_axes_in_gurobis_wording() -> None:
    with lps.solve(INFEASIBLE, DATA['INFEASIBLE'], solver_name='gurobi') as solution:
        assert solution.status == 'warning'
        assert solution.termination_condition == 'infeasible'
        assert not solution.has_primal
        assert solution.objective != solution.objective  # nan, not 0.0
        with pytest.raises(NoSolutionError, match='INFEASIBLE'):
            solution.primal('p')


def test_a_mixed_integer_model_has_no_duals() -> None:
    """Gurobi refuses ``Pi`` rather than returning zeros; the sink passes the
    refusal on as the ``None`` that makes ``dual`` explain itself."""
    with lps.solve(MIP, DATA['MIP'], solver_name='gurobi') as solution:
        assert solution.has_primal
        with pytest.raises(LpspecError, match='mixed-integer'):
            solution.dual('budget')


def test_solver_options_reach_gurobi() -> None:
    """Verbatim, in Gurobi's own vocabulary — ``TimeLimit``, not HiGHS'
    ``time_limit``. Forwarding is the contract; translating names is not, and
    an option the solver does not know reaches the caller as the solver's own
    complaint rather than as a guess at what was meant."""
    with lps.solve(MIP, DATA['MIP'], solver_options={'TimeLimit': 0.0}, solver_name='gurobi') as solution:
        assert solution.termination_condition == 'time_limit'
    with pytest.raises(gurobipy.GurobiError, match='no_such_parameter'):
        lps.solve(MIP, DATA['MIP'], solver_options={'no_such_parameter': 1}, solver_name='gurobi')


# ---------------------------------------------------------------------------
# the seams: the build without the search, and choosing a sink at all
# ---------------------------------------------------------------------------


def test_solver_options_land_on_the_environment() -> None:
    """Where a licence parameter has to go.

    ``WLSAccessID`` / ``ComputeServer`` / ``TokenServer`` can only be set
    before an environment starts, so applying options to the *model* — as this
    sink first did — locks out every Compute-Server and WLS user. Asserted
    through an ordinary parameter, since a licence one would need a licence:
    the model sees it as its default, which is what environment-level means.
    """
    with lps.build(MIP, DATA['MIP']) as ex:
        assert build_gurobi(ex._tables(), solver_options={'TimeLimit': 5.0}).Params.TimeLimit == 5.0


def test_build_gurobi_loads_the_model_and_stops() -> None:
    """`bench/`'s seam: the hand-off with no search behind it, so what it
    reports is what was loaded rather than what was solved."""
    with lps.build(MIP, DATA['MIP']) as ex:
        tables = ex._tables()
        m = build_gurobi(tables)
        assert (m.NumVars, m.NumConstrs) == (tables.column_count, tables.row_count)
        assert m.NumIntVars == tables.cols.filter(pl.col('vtype') != 'continuous').height
        assert m.ModelSense == gurobipy.GRB.MAXIMIZE
        assert m.SolCount == 0


def test_the_objective_constant_rides_on_the_model_not_the_answer() -> None:
    """Gurobi has ``ObjCon``, so the constant is part of the model it holds —
    which makes the build seam a complete hand-off rather than a model plus a
    number to remember."""
    with lps.build(MAX, DATA['MAX']) as ex:
        assert build_gurobi(ex._tables()).ObjCon == pytest.approx(5.0)


def test_the_missing_extra_is_named() -> None:
    """What a caller without gurobipy meets — both halves named, since the
    absent one is as often scipy."""
    real_import = builtins.__import__

    def refuse(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in {'gurobipy', 'scipy.sparse'}:
            raise ModuleNotFoundError(f'No module named {name!r}')
        return real_import(name, *args, **kwargs)

    with lps.build(LP, DATA['LP']) as ex, pytest.MonkeyPatch.context() as patch:
        patch.setattr(builtins, '__import__', refuse)
        with pytest.raises(ModuleNotFoundError, match=r'\[gurobi\] extra \(gurobipy, scipy\)'):
            ex.solve(solver_name='gurobi')
