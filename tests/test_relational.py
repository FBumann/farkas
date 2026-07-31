"""Phase-2 gate: two real models round-trip through solve on the relational backend.

Each model is built three ways and must agree on the objective:
  1. relational executor -> the `highs` solver (batched addCols/addRows)
  2. relational executor -> lp_file sink -> HiGHS reads and solves the file
  3. eager linopy build (the correctness oracle)
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import polars as pl
import pytest

import lpspec as lps
from lpspec.errors import DataError, LanguageError, LpspecError
from lpspec.language.schema import MathSchema
from lpspec.lowering import lower_program
from lpspec.relational import (
    PolarsExecutor,
    chunking,
)
from lpspec.relational.engines.polars.executor import _needs_aggregate
from lpspec.relational.plan import (
    Constant,
    ConstraintDeclaration,
    DimensionDeclaration,
    GroupSum,
    ObjectiveDeclaration,
    Parameter,
    ParameterComparison,
    ParameterDeclaration,
    Program,
    Sum,
    Variable,
    VariableDeclaration,
)
from lpspec.relational.sinks import SOLVERS
from tests.conftest import by_coord, solve_lp_file
from tests.differential import RTOL, differential
from tests.oracle import linopy, pd, transport_eager_objective, xr

# ---------------------------------------------------------------------------
# model 1: dispatch (the spec example)
# ---------------------------------------------------------------------------


@pytest.fixture
def dispatch_data():
    rng = np.random.default_rng(7)
    n_s, n_g = 40, 6
    gens = pd.DataFrame(
        {
            'generator': [f'g{i}' for i in range(n_g)],
            'p_max': rng.uniform(50, 200, n_g).round(3),
            'cost': rng.uniform(5, 100, n_g).round(3),
        }
    )
    gens.loc[1, 'p_max'] = 0.0  # masked out by where
    load = pd.DataFrame(
        {
            'snapshot': np.arange(n_s),
            'value': (rng.uniform(0.2, 0.7, n_s) * gens['p_max'].sum()).round(3),
        }
    )
    return gens, load


def dispatch_program() -> Program:
    return Program(
        parameters=(
            ParameterDeclaration('p_max', ('generator',)),
            ParameterDeclaration('cost', ('generator',)),
            ParameterDeclaration('load', ('snapshot',)),
        ),
        variables=(
            VariableDeclaration(
                'p',
                ('snapshot', 'generator'),
                where=ParameterComparison('p_max', '>', 0),
                lower=Constant(0.0),
                upper=Parameter('p_max'),
            ),
        ),
        constraints=(
            ConstraintDeclaration(
                'power_balance',
                ('snapshot',),
                lhs=Sum(Variable('p'), over=('generator',)),
                sense='==',
                rhs=Parameter('load'),
            ),
        ),
        objective=ObjectiveDeclaration('min', Sum(Variable('p') * Parameter('cost'), over=('generator', 'snapshot'))),
    )


def dispatch_sources(gens: pd.DataFrame, load: pd.DataFrame) -> dict:
    return {
        'p_max': gens[['generator', 'p_max']].rename(columns={'p_max': 'value'}),
        'cost': gens[['generator', 'cost']].rename(columns={'cost': 'value'}),
        'load': load,
        'snapshot': load[['snapshot']],
    }


def dispatch_eager_objective(gens: pd.DataFrame, load: pd.DataFrame) -> float:
    gi = gens.set_index('generator')
    li = load.set_index('snapshot')['value']
    p_max = xr.DataArray.from_series(gi['p_max'])
    cost = xr.DataArray.from_series(gi['cost'])
    load_da = xr.DataArray.from_series(li)

    m = linopy.Model()
    mask = (p_max > 0).broadcast_like(load_da * p_max)
    p = m.add_variables(lower=0, upper=p_max, coords=[li.index, gi.index], name='p', mask=mask)
    m.add_constraints(p.sum('generator') == load_da, name='power_balance')
    m.add_objective((p * cost).sum())
    m.solve(solver_name='highs', output_flag=False)
    return float(m.objective.value)


def test_dispatch_roundtrip(dispatch_data, tmp_path):
    gens, load = dispatch_data
    oracle = dispatch_eager_objective(gens, load)

    with PolarsExecutor() as ex:
        ex.build(dispatch_program(), dispatch_sources(gens, load))

        result = ex.solve()
        assert result.is_ok
        assert result.objective == pytest.approx(oracle, rel=RTOL)

        lp = tmp_path / 'dispatch.lp'
        ex.write(lp)
        assert solve_lp_file(lp) == pytest.approx(oracle, rel=RTOL)

        # masked variable rows are absent, and primal joins back to coords
        primal = result.to_pandas('p')
        n_active = int((gens['p_max'] > 0).sum())
        assert len(primal) == n_active * len(load)
        assert set(primal.columns) == {'snapshot', 'generator', 'value'}
        # per-snapshot dispatch matches load
        balance = primal.groupby('snapshot')['value'].sum()
        expected = load.set_index('snapshot')['value']
        assert np.allclose(balance.sort_index(), expected.sort_index())


# ---------------------------------------------------------------------------
# model 2: multi-bus transport (exercises GroupSum and signed flows)
# ---------------------------------------------------------------------------


def transport_program() -> Program:
    injection = (
        GroupSum(Variable('p'), over='generator', coordinate='bus', into='bus')
        + GroupSum(Variable('f'), over='line', coordinate='to', into='bus')
        - GroupSum(Variable('f'), over='line', coordinate='from', into='bus')
    )
    return Program(
        parameters=(
            ParameterDeclaration('p_max', ('generator',)),
            ParameterDeclaration('cost', ('generator',)),
            ParameterDeclaration('cap', ('line',)),
            ParameterDeclaration('load', ('snapshot', 'bus')),
        ),
        variables=(
            VariableDeclaration(
                'p',
                ('snapshot', 'generator'),
                lower=Constant(0.0),
                upper=Parameter('p_max'),
            ),
            VariableDeclaration(
                'f',
                ('snapshot', 'line'),
                lower=-Parameter('cap'),
                upper=Parameter('cap'),
            ),
        ),
        constraints=(
            ConstraintDeclaration(
                'balance',
                ('snapshot', 'bus'),
                lhs=injection,
                sense='==',
                rhs=Parameter('load'),
            ),
        ),
        objective=ObjectiveDeclaration('min', Sum(Variable('p') * Parameter('cost'), over=('generator', 'snapshot'))),
        dimensions=(
            DimensionDeclaration('generator', (('bus', 'bus'),)),
            DimensionDeclaration('line', (('from', 'bus'), ('to', 'bus'))),
        ),
    )


def transport_sources(gens, lines, load) -> dict:
    return {
        'p_max': gens[['generator', 'p_max']].rename(columns={'p_max': 'value'}),
        'cost': gens[['generator', 'cost']].rename(columns={'cost': 'value'}),
        'cap': lines[['line', 'cap']].rename(columns={'cap': 'value'}),
        'load': load,
        'snapshot': load[['snapshot']],
        'bus': load[['bus']],
        # dims carrying declared coordinates need an index source that has them
        'generator': gens[['generator', 'bus']],
        'line': lines[['line', 'from_bus', 'to_bus']].rename(columns={'from_bus': 'from', 'to_bus': 'to'}),
    }


def test_transport_roundtrip(transport_data, tmp_path):
    gens, lines, load = transport_data
    oracle = transport_eager_objective(gens, lines, load)
    assert np.isfinite(oracle), 'oracle model must be feasible'

    with PolarsExecutor() as ex:
        ex.build(transport_program(), transport_sources(gens, lines, load))

        result = ex.solve()
        assert result.is_ok
        assert result.objective == pytest.approx(oracle, rel=RTOL)

        lp = tmp_path / 'transport.lp'
        ex.write(lp)
        assert solve_lp_file(lp) == pytest.approx(oracle, rel=RTOL)

        # flows respect line capacity bounds
        primal_f = result.to_pandas('f')
        caps = lines.set_index('line')['cap']
        limits = primal_f['line'].map(caps)
        assert (primal_f['value'].abs() <= limits + 1e-6).all()


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


def test_nonlinear_product_rejected(dispatch_data):
    gens, load = dispatch_data
    prog = dispatch_program()
    bad = Program(
        parameters=prog.parameters,
        variables=prog.variables,
        constraints=prog.constraints,
        objective=ObjectiveDeclaration('min', Sum(Variable('p') * Variable('p'), over=('generator', 'snapshot'))),
    )
    with PolarsExecutor() as ex, pytest.raises(LanguageError, match='nonlinear'):
        ex.build(bad, dispatch_sources(gens, load))


def test_missing_source_rejected(dispatch_data):
    gens, load = dispatch_data
    sources = dispatch_sources(gens, load)
    del sources['cost']
    with PolarsExecutor() as ex, pytest.raises(DataError, match="no source bound for parameter 'cost'"):
        ex.build(dispatch_program(), sources)


#: A scalar parameter used in a bound and in the objective — the two places a
#: silent row multiplication is least visible.
SCALAR_MODEL = {
    'dimensions': {'i': {'dtype': 'int', 'values': [0, 1]}},
    'parameters': {'s': {'dims': []}},
    'variables': {'x': {'foreach': ['i'], 'bounds': {'lower': 0, 'upper': 's'}}},
    'constraints': {'floor': {'foreach': ['i'], 'expression': 'x >= 1'}},
    'objectives': {'total': {'sense': 'minimize', 'expression': 'sum(x * s, over=i)'}},
}


@pytest.mark.parametrize('rows', [2, 0])
def test_a_dimensionless_parameter_must_be_one_row(rows):
    """No dims means one value broadcast everywhere, and nothing used to check it.

    A dimensionless parameter is broadcast by joining on nothing, which is
    right for one row and a silent row multiplication for two — duplicate
    columns for one variable in a bound, duplicate mask rows in a where. The
    per-coordinate check skipped this case entirely, because a parameter with
    no dims has nothing to group by, which also left `keyed` claiming the
    opposite of what such a source held (#166).
    """
    data = {'s': pl.DataFrame({'value': [1.0] * rows}, schema={'value': pl.Float64})}
    with pytest.raises(DataError, match=f"parameter 's' .* its source has {rows} rows"):
        lps.build(SCALAR_MODEL, data)


def test_a_dimensionless_parameter_of_one_row_still_builds():
    """The control: the shape the check exists to let through."""
    data = {'s': pl.DataFrame({'value': [10.0]})}
    with lps.solve(SCALAR_MODEL, data) as result:
        assert result.objective == pytest.approx(20.0)  # x == 1 at both coordinates of i, times s


def test_out_of_foreach_dims_rejected(dispatch_data):
    gens, load = dispatch_data
    prog = dispatch_program()
    bad = Program(
        parameters=prog.parameters,
        variables=prog.variables,
        constraints=(
            ConstraintDeclaration(
                'power_balance',
                ('snapshot',),
                lhs=Variable('p'),  # generator dim not summed
                sense='==',
                rhs=Parameter('load'),
            ),
        ),
        objective=prog.objective,
    )
    with PolarsExecutor() as ex, pytest.raises(LanguageError, match='missing a Sum'):
        ex.build(bad, dispatch_sources(gens, load))


def test_an_awkward_path_is_a_value_not_syntax(tmp_path):
    """Paths come from the calling program, so no language rule constrains them.

    ``o'brien`` is a legal directory name, and a quote in one must be as
    uninteresting as a quote in a label. Every path-carrying sink and source is
    exercised here: a parquet source, an explicit index source, the LP writer,
    and the parquet sink.
    """
    odd = tmp_path / "o'brien"
    odd.mkdir()
    pl.DataFrame({'snapshot': [0, 1], 'value': [1.0, 2.0]}).write_parquet(odd / 'load.parquet')
    pl.DataFrame({'snapshot': [0, 1]}).write_parquet(odd / 'index.parquet')

    model = {
        'dimensions': {'snapshot': {'dtype': 'int'}},
        'parameters': {'load': {'dims': ['snapshot']}},
        'variables': {'p': {'foreach': ['snapshot'], 'bounds': {'lower': 0}}},
        'constraints': {'meet': {'foreach': ['snapshot'], 'expression': 'p >= load'}},
        'objectives': {'c': {'sense': 'minimize', 'expression': 'sum(p, over=snapshot)'}},
    }
    sources = {'load': str(odd / 'load.parquet'), 'snapshot': str(odd / 'index.parquet')}

    lps.write(model, sources, odd / 'model.lp')
    result = lps.solve(model, sources)
    assert result.objective == pytest.approx(3.0)
    assert set(result.to_parquet(odd / 'solution')) == {'p'}


def test_a_variable_appearing_twice_in_a_row_is_summed_not_duplicated():
    """The case the skipped aggregate must not break.

    `x + 2 * x` is two term fragments landing on one solver column, so the
    assembly has to add them. Its coefficient must be 3, and the row must hold
    one entry for that column rather than two — a solver handed the same
    column twice in one row is entitled to reject the model.
    """
    model = {
        'dimensions': {'i': {'dtype': 'int', 'values': [0, 1]}},
        'parameters': {'rhs': {'dims': ['i']}},
        'variables': {'x': {'foreach': ['i'], 'bounds': {'lower': 0}}},
        'constraints': {'c': {'foreach': ['i'], 'expression': 'x + 2 * x >= rhs'}},
        'objectives': {'o': {'sense': 'minimize', 'expression': 'sum(x, over=i)'}},
    }
    sources = {'rhs': pl.DataFrame({'i': [0, 1], 'value': [6.0, 9.0]})}
    with lps.build(model, sources) as ex:
        matrix = ex._tables().matrix
        assert matrix.height == 2  # one entry per row, not one per fragment
        assert sorted(matrix['coeff'].to_list()) == [3.0, 3.0]
        result = ex.solve()
    assert result.objective == pytest.approx(5.0)  # 6/3 + 9/3


def test_a_factored_mask_labels_exactly_like_the_counted_path():
    """The fast path has to be indistinguishable from the one it replaces.

    A mask that reads none of the leading dims removes the same coordinates
    under every one of their values, so the survivors are a rectangle and the
    label is arithmetic. That is only a speedup if it lands on the *same*
    numbers the counted path would have assigned — a label is the solver's own
    column index, so an off-by-one here is a different model, not a slower one.

    Both paths are run over the same model and compared outright. The mask
    reads `tech` only, so it factors; forcing it through the counted path is a
    matter of asking for the labels the sort produces.
    """
    import polars as pl_

    from lpspec.relational.engines.polars.compiler import _ordinal

    model = {
        'dimensions': {
            'snapshot': {'dtype': 'int', 'values': list(range(5))},
            'node': {'dtype': 'str', 'values': ['a', 'b', 'c']},
            'tech': {'dtype': 'str', 'values': ['wind', 'gas', 'coal', 'hydro']},
        },
        'parameters': {'cap': {'dims': ['node', 'tech']}, 'load': {'dims': ['snapshot']}},
        'variables': {
            'p': {'foreach': ['snapshot', 'node', 'tech'], 'where': 'cap > 0', 'bounds': {'lower': 0, 'upper': 'cap'}}
        },
        'constraints': {
            'balance': {
                'foreach': ['snapshot', 'node'],
                'expression': 'sum(p, over=tech) >= load',
            }
        },
        'objectives': {
            'o': {
                'sense': 'minimize',
                'expression': 'sum(sum(sum(p, over=tech), over=node), over=snapshot)',
            }
        },
    }
    caps = [
        {'node': n, 'tech': t, 'value': 0.0 if (n, t) in {('a', 'gas'), ('c', 'coal'), ('b', 'hydro')} else 10.0}
        for n in ['a', 'b', 'c']
        for t in ['wind', 'gas', 'coal', 'hydro']
    ]
    sources = {
        'cap': pl.DataFrame(caps),
        'load': pl.DataFrame({'snapshot': list(range(5)), 'value': [1.0] * 5}),
    }

    with lps.build(model, sources) as ex:
        fast = ex._variables['p'].collect().sort('var_label')
        dims = ('snapshot', 'node', 'tech')
        counted = (
            ex._q.frame(dims, ex._program.variables[0].where)
            .sort([_ordinal(d) for d in dims])
            .select(*dims)
            .with_row_index('var_label', offset=0)
            .select(*dims, pl_.col('var_label').cast(pl_.Int64))
            .collect()
        )

    assert fast.height == counted.height, 'the two paths disagree about how many coordinates survive'
    assert fast.equals(counted.sort('var_label')), 'the factored labels are not the counted labels'
    assert fast['var_label'].to_list() == list(range(fast.height)), 'labels must stay dense from 0'


def test_a_dictionary_encoded_source_column_binds_like_a_plain_one():
    """A `Categorical` dim column is a source encoding, not a different model.

    Any writer that sees a 12M-row table of repeated node names will
    dictionary-encode it, and pandas does it by default for a `Categorical`.
    polars will not join `Categorical` against `String`, and the dim frames are
    built from declared coordinate values and are plain — so without a cast the
    two agree only by luck, and the failure is a schema error from inside a
    join rather than anything a caller can act on.
    """
    model = {
        'dimensions': {'node': {'dtype': 'str', 'values': ['a', 'b']}},
        'parameters': {'cap': {'dims': ['node']}},
        'variables': {'x': {'foreach': ['node'], 'bounds': {'lower': 0, 'upper': 'cap'}}},
        'constraints': {'c': {'foreach': ['node'], 'expression': 'x >= cap'}},
        'objectives': {'o': {'sense': 'minimize', 'expression': 'sum(x, over=node)'}},
    }
    encoded = pl.DataFrame({'node': ['a', 'b'], 'value': [3.0, 4.0]}).with_columns(pl.col('node').cast(pl.Categorical))
    plain = pl.DataFrame({'node': ['a', 'b'], 'value': [3.0, 4.0]})

    with lps.build(model, {'cap': encoded}) as ex:
        from_encoded = ex.solve().objective
    with lps.build(model, {'cap': plain}) as ex:
        from_plain = ex.solve().objective

    assert from_encoded == pytest.approx(7.0)
    assert from_encoded == pytest.approx(from_plain), 'the encoding changed the model'


def test_an_objective_naming_a_variable_twice_sums_its_coefficients():
    """Same argument, one dimension down: the objective is a column vector."""
    model = {
        'dimensions': {'i': {'dtype': 'int', 'values': [0]}},
        'parameters': {'lb': {'dims': ['i']}},
        'variables': {'x': {'foreach': ['i'], 'bounds': {'lower': 'lb'}}},
        'constraints': {'c': {'foreach': ['i'], 'expression': 'x >= lb'}},
        'objectives': {'o': {'sense': 'minimize', 'expression': 'x + 4 * x'}},
    }
    with lps.build(model, {'lb': pl.DataFrame({'i': [0], 'value': [2.0]})}) as ex:
        assert ex._tables().obj.height == 1
        assert ex._tables().obj['coeff'].to_list() == [5.0]
        assert ex.solve().objective == pytest.approx(10.0)


def test_a_mask_that_removes_nothing_labels_exactly_like_no_mask(dispatch_data):
    """A vacuous `where` must not shift a single solver index.

    Labels are the solver's own column numbers, so "the mask removed nothing"
    and "there was no mask" have to produce identical frames rather than merely
    the same row count.
    """
    gens, load = dispatch_data
    gens = gens.assign(p_max=gens['p_max'].where(gens['p_max'] > 0, 1.0))  # nothing left to mask out

    labels = []
    for where in (None, ParameterComparison('p_max', '>', 0)):
        base = dispatch_program()
        program = replace(base, variables=(replace(base.variables[0], where=where),))
        with PolarsExecutor() as ex:
            ex.build(program, dispatch_sources(gens, load))
            labels.append(ex._variables['p'].collect().sort('var_label'))
    assert labels[0].equals(labels[1])


def _objective_of(program, sources):
    """`obj` as `{col: coeff}`, plus whether the aggregate was skipped."""
    with PolarsExecutor() as ex:
        ex.build(program, sources)
        obj = ex._tables().obj
        return dict(zip(obj['col'].to_list(), obj['coeff'].to_list(), strict=True)), obj.height


def test_a_mask_a_missing_value_can_satisfy_keeps_the_rows_with_no_value():
    """`not` and `or` select rows the mask's own join must not have dropped.

    A mask's parameters are joined for, and under a plain conjunction that join
    can be an inner one: a coordinate the parameter has no row for fails the
    conjunct anyway, so keeping it only widens the frame the filter then
    narrows. Under `not` or `or` a missing value is what makes the mask *true*,
    and an inner join would have thrown the row away before the filter could
    say so.

    Every mask in the suite was a conjunction before this, so nothing else here
    distinguishes the two joins.
    """
    model = {
        'dimensions': {'i': {'dtype': 'int', 'values': [0, 1, 2, 3]}},
        'parameters': {'a': {'dims': ['i']}, 'b': {'dims': ['i']}},
        'variables': {
            'absent': {'foreach': ['i'], 'where': 'not a', 'bounds': {'lower': 0, 'upper': 1}},
            'either': {'foreach': ['i'], 'where': 'a > 0 or b > 0', 'bounds': {'lower': 0, 'upper': 1}},
            'both': {'foreach': ['i'], 'where': 'a and a > 0', 'bounds': {'lower': 0, 'upper': 1}},
            'mixed': {'foreach': ['i'], 'where': 'a and not b', 'bounds': {'lower': 0, 'upper': 1}},
        },
        'objectives': {'o': {'sense': 'minimize', 'expression': 'sum(absent, over=i)'}},
    }
    sources = {
        'a': pl.DataFrame({'i': [0, 1], 'value': [5.0, -1.0]}),
        'b': pl.DataFrame({'i': [2], 'value': [7.0]}),
    }
    with lps.build(model, sources) as ex:
        surviving = {
            name: sorted(ex._variables[name].select('i').collect().to_series().to_list())
            for name in ('absent', 'either', 'both', 'mixed')
        }
    assert surviving == {
        'absent': [2, 3],  # a is missing there, which is the whole condition
        'either': [0, 2],  # i=2 has no `a` at all and qualifies on `b`
        'both': [0],
        'mixed': [0, 1],  # `a` is certain, `b` is not
    }


def test_every_declaration_owns_a_contiguous_run_of_labels():
    """What reading a solve back by position rests on.

    A solver vector is positional in the same index the labels are, so a
    declaration's share of it is a slice — but only if its labels are a dense
    run and the runs tile the whole index. Both are true of every path
    `Labeller.frame` takes, and neither is visible from the frames: a block
    that started one late would report a neighbour's numbers under this
    declaration's coordinates, with nothing out of range and nothing null.
    """
    model = {
        'dimensions': {'i': {'dtype': 'int', 'values': [0, 1, 2]}, 'j': {'values': ['a', 'b']}},
        'parameters': {'cap': {'dims': ['i']}},
        'variables': {
            'x': {'foreach': ['i'], 'bounds': {'lower': 0, 'upper': 'cap'}},
            'y': {'foreach': ['i', 'j'], 'bounds': {'lower': 0}},
            'z': {'foreach': ['j'], 'bounds': {'lower': 0}},
        },
        'constraints': {
            'c1': {'foreach': ['i'], 'expression': 'x >= cap'},
            'c2': {'foreach': ['i', 'j'], 'expression': 'y >= 0'},
        },
        'objectives': {'o': {'sense': 'minimize', 'expression': 'sum(x, over=i)'}},
    }
    with lps.build(model, {'cap': pl.DataFrame({'i': [0, 1, 2], 'value': [1.0, 2.0, 3.0]})}) as ex:
        tables = ex._tables()
        for names, total, frames, label in (
            (['x', 'y', 'z'], tables.column_count, ex._variables, 'var_label'),
            (['c1', 'c2'], tables.row_count, ex._constraints, 'row'),
        ):
            at = 0
            for name in names:
                start, height = ex._blocks[name]
                assert start == at, f'{name} does not start where the previous declaration ended'
                labels = frames[name].select(label).collect().to_series()
                assert sorted(labels) == list(range(start, start + height)), f'{name} is not a dense run'
                at += height
            assert at == total, 'the runs do not tile the index'


def test_the_matrix_aggregate_runs_on_what_repeats_and_not_on_what_might():
    """Both branches of the collapse, on models that differ only in overlap.

    `_needs_aggregate` reads the fragments and can only say whether a cell
    *can* repeat. Two fragments over disjoint variables satisfy it and repeat
    nothing; two over the same variable repeat every cell. The first must come
    out untouched and the second must be summed — the point being that the
    same static answer covers both, so the frame itself has to decide.
    """
    base = {
        'dimensions': {'i': {'dtype': 'int', 'values': [0, 1]}},
        'parameters': {'rhs': {'dims': ['i']}},
        'variables': {
            'x': {'foreach': ['i'], 'bounds': {'lower': 0}},
            'y': {'foreach': ['i'], 'bounds': {'lower': 0}},
        },
        'objectives': {'o': {'sense': 'minimize', 'expression': 'sum(x, over=i) + sum(y, over=i)'}},
    }
    sources = {'rhs': pl.DataFrame({'i': [0, 1], 'value': [4.0, 6.0]})}

    disjoint = dict(base, constraints={'c': {'foreach': ['i'], 'expression': 'x + y >= rhs'}})
    with lps.build(disjoint, sources) as ex:
        matrix = ex._tables().matrix
        assert matrix.height == 4, 'two variables per row, nothing to collapse'
        assert matrix['coeff'].to_list() == [1.0, 1.0, 1.0, 1.0]

    overlapping = dict(base, constraints={'c': {'foreach': ['i'], 'expression': 'x + 3 * x >= rhs'}})
    with lps.build(overlapping, sources) as ex:
        matrix = ex._tables().matrix
        assert matrix.height == 2, 'one cell per row after the collapse'
        assert matrix['coeff'].to_list() == [4.0, 4.0]


def test_stacking_distinct_variables_asks_for_no_aggregate():
    """The static answer itself, which no end-to-end assertion can reach.

    `test_the_matrix_aggregate_runs_on_what_repeats_and_not_on_what_might`
    pins the *rows*, and both branches produce the same ones — a build that
    sorts every nonzero to collapse nothing is indistinguishable from one that
    never sorted. So the question of whether the aggregate was asked for has
    to be put to `_needs_aggregate` directly.

    Reading the count alone answers 'yes' to every multi-term constraint,
    which is most of them.
    """
    dims = {'i': {'dtype': 'int', 'values': [0, 1]}}
    variables = {'x': {'foreach': ['i'], 'bounds': {'lower': 0}}, 'y': {'foreach': ['i'], 'bounds': {'lower': 0}}}
    sources = {'rhs': pl.DataFrame({'i': [0, 1], 'value': [4.0, 6.0]})}

    def fragments(expression):
        program = lower_program(
            MathSchema(
                dimensions=dims,
                parameters={'rhs': {'dims': ['i']}},
                variables=variables,
                constraints={'c': {'foreach': ['i'], 'expression': f'{expression} >= rhs'}},
                objectives={'o': {'sense': 'minimize', 'expression': 'sum(x, over=i)'}},
            )
        )
        with PolarsExecutor() as ex:
            ex.build(program, sources)
            return ex._q.expression(program.constraints[0].lhs, 'test').terms

    assert not _needs_aggregate(fragments('x + y'))
    assert _needs_aggregate(fragments('x + 3 * x'))


def test_the_objective_skips_the_aggregate_only_when_a_column_cannot_repeat():
    """`p * cost` needs no aggregate; `p * cost + p * cost` does."""
    base = {
        'dimensions': {'i': {'dtype': 'int', 'values': [0, 1]}},
        'parameters': {'cost': {'dims': ['i']}, 'lb': {'dims': ['i']}},
        'variables': {'p': {'foreach': ['i'], 'bounds': {'lower': 'lb'}}},
        'constraints': {'c': {'foreach': ['i'], 'expression': 'p >= lb'}},
    }
    sources = {
        'cost': pl.DataFrame({'i': [0, 1], 'value': [2.0, 3.0]}),
        'lb': pl.DataFrame({'i': [0, 1], 'value': [1.0, 1.0]}),
    }
    once = lower_program(MathSchema(**dict(base, objectives={'o': {'sense': 'minimize', 'expression': 'p * cost'}})))
    twice = lower_program(
        MathSchema(**dict(base, objectives={'o': {'sense': 'minimize', 'expression': 'p * cost + p * cost'}}))
    )

    assert _objective_of(once, sources) == ({0: 2.0, 1: 3.0}, 2)
    assert _objective_of(twice, sources) == ({0: 4.0, 1: 6.0}, 2)


def test_the_objective_keeps_the_aggregate_when_a_reduction_hides_extra_rows():
    """A fragment's dims can match the variable's while its rows do not.

    `sum(q * price, over=generator)` with `q` indexed by snapshot alone reduces
    to dims `('snapshot',)` — exactly `q`'s declaration — but `_sum_fragment`
    *projects*, so the fragment still carries one row per generator. Skipping
    the aggregate there writes |generator| rows into `obj` for a single column:
    the LP file would quietly re-sum them, while `cols` joined to `obj` in the
    HiGHS sink would hand the solver more columns than the model has.

    This is the case a dims-equality test gets wrong, and the reason a fragment
    tracks which of its dims its label actually determines.
    """
    model = {
        'dimensions': {'snapshot': {'dtype': 'int', 'values': [0, 1]}, 'generator': {'values': ['g0', 'g1', 'g2']}},
        'parameters': {'price': {'dims': ['snapshot', 'generator']}, 'load': {'dims': ['snapshot']}},
        'variables': {'q': {'foreach': ['snapshot'], 'bounds': {'lower': 0, 'upper': 10}}},
        'constraints': {'floor': {'foreach': ['snapshot'], 'expression': 'q >= load'}},
        'objectives': {'o': {'sense': 'minimize', 'expression': 'sum(q * price, over=generator)'}},
    }
    sources = {
        'price': pl.DataFrame(
            {'snapshot': [0, 0, 0, 1, 1, 1], 'generator': ['g0', 'g1', 'g2'] * 2, 'value': [1.0, 2.0, 3.0] * 2}
        ),
        'load': pl.DataFrame({'snapshot': [0, 1], 'value': [5.0, 5.0]}),
    }
    # one row per column, each carrying the summed price — not three rows of one
    assert _objective_of(lower_program(MathSchema(**model)), sources) == ({0: 6.0, 1: 6.0}, 2)


def test_infinite_bounds_survive_the_handoff(dispatch_data):
    """An absent upper bound must reach HiGHS as infinity, not as a number."""
    gens, load = dispatch_data
    base = dispatch_program()
    unbounded = replace(base, variables=(replace(base.variables[0], upper=Constant(float('inf'))),))
    with PolarsExecutor() as ex:
        ex.build(unbounded, dispatch_sources(gens, load))
        assert ex._tables().cols['ub'].is_infinite().all()
        assert ex.solve().is_ok


@pytest.mark.parametrize('length', [2, 5], ids=['short', 'long'])
def test_a_solver_vector_that_does_not_span_the_model_is_refused(monkeypatch, length):
    """A wrong length is a different model's answer, not a short one.

    Reading a solution back is positional. A short vector leaves the trailing
    declarations reading past the end; a long one leaves every declaration
    reading the right slice of the wrong vector. Neither is recoverable, so
    both are refused where the solver hands them over — not where they are
    read: the objective comes straight from the solver, so a `Result` built on
    a broken vector reports a plausible number and fails only if someone asks
    for a coordinate.
    """
    from lpspec.relational.engines.polars import executor as executor_module

    model = {
        'dimensions': {'t': {'dtype': 'int', 'values': [0, 1, 2]}},
        'parameters': {'load': {'dims': ['t']}},
        'variables': {'x': {'foreach': ['t'], 'bounds': {'lower': 0, 'upper': 10}}},
        'constraints': {'meet': {'foreach': ['t'], 'expression': 'x >= load'}},
        'objectives': {'o': {'sense': 'minimize', 'expression': 'sum(x, over=t)'}},
    }
    honest = executor_module.sinks.solver('highs')

    def crooked(tables, batch_rows, options):
        status, objective, primal, dual = honest(tables, batch_rows, options)
        stretched = pl.Series('value', list(primal) + [0.0] * length)
        return status, objective, stretched.head(length), dual

    monkeypatch.setattr(executor_module.sinks, 'solver', lambda _name: crooked)
    with (
        lps.build(model, {'load': pl.DataFrame({'t': [0, 1, 2], 'value': [1.0, 2.0, 3.0]})}) as ex,
        pytest.raises(LpspecError, match=f'returned {length} primal values for a model with 3'),
    ):
        ex.solve()


@pytest.mark.parametrize('solver_name', sorted(SOLVERS))
def test_a_solver_hands_back_a_vector_and_not_an_index(solver_name):
    """A solution is positional, so there is nothing to key it by.

    Solver output is indexed by the solver's own index, which *is* our label —
    so a ``(label, value)`` frame carries an ``arange`` beside every value that
    the read-back never reads, 8 bytes a column for as long as the result is
    held. The same argument took ``col`` off ``cols`` in #433; this is the
    other half of it, and neither is visible from the numbers.
    """
    model = {
        'dimensions': {'t': {'dtype': 'int', 'values': [0, 1, 2]}},
        'parameters': {'load': {'dims': ['t']}},
        'variables': {'x': {'foreach': ['t'], 'bounds': {'lower': 0, 'upper': 10}}},
        'constraints': {'meet': {'foreach': ['t'], 'expression': 'x >= load'}},
        'objectives': {'o': {'sense': 'minimize', 'expression': 'sum(x, over=t)'}},
    }
    with lps.build(model, {'load': pl.DataFrame({'t': [0, 1, 2], 'value': [1.0, 2.0, 3.0]})}) as ex:
        tables = ex._tables()
        solution = ex.solve(solver_name=solver_name)
        assert solution.is_ok
        for values, count in (
            (solution._primal_values, tables.column_count),
            (solution._dual_values, tables.row_count),
        ):
            assert isinstance(values, pl.Series), 'a frame here is an index column nothing reads'
            assert values.name == 'value'
            assert len(values) == count, 'the read-back slices it positionally, so it spans the model'


@pytest.mark.parametrize('solver_name', sorted(SOLVERS))
@pytest.mark.parametrize('batch_rows', [1, 2, 7, 100_000], ids=['one', 'two', 'odd', 'whole'])
def test_a_row_with_no_terms_keeps_its_seat_at_any_chunking(solver_name, batch_rows):
    """Rows reach a solver by position, so an empty one still occupies one.

    `where: "t > 0"` leaves `balance` at `t = 0` with nothing to sum, and this
    lane keeps the row: `0 == 5` is infeasible and says so. Both solvers now
    take the row bounds from a dense vector and slice it per chunk, which is
    the same hand-off only if every label in the range has a seat — a row that
    fell out of the frame would take a comparison nothing can fail, leave the
    constraint unenforced, and the model would come back solved against a
    model that cannot be.

    Ragged batches because the range loop is where a seat would be lost, and a
    round number is the one split that hides an off-by-one. Both solvers,
    because the seating is now theirs jointly rather than either one's.
    """
    model = {
        'dimensions': {'t': {'dtype': 'int', 'values': [0, 1, 2]}, 'g': {'values': ['a', 'b']}},
        'parameters': {'load': {'dims': ['t']}},
        'variables': {'p': {'foreach': ['t', 'g'], 'where': 't > 0', 'bounds': {'lower': 0, 'upper': 100}}},
        'constraints': {'balance': {'foreach': ['t'], 'expression': 'sum(p, over=g) == load'}},
        'objectives': {'o': {'sense': 'minimize', 'expression': 'sum(sum(p, over=g), over=t)'}},
    }
    with lps.build(model, {'load': pl.DataFrame({'t': [0, 1, 2], 'value': [5.0, 4.0, 6.0]})}) as ex:
        assert sorted(set(ex._tables().matrix['row'].to_list())) == [1, 2], 'row 0 is the orphan under test'
        solution = ex.solve(batch_rows=batch_rows, solver_name=solver_name)
        assert solution.termination_condition == 'infeasible'


def test_row_chunks_are_bounded_by_nonzeros_not_by_rows():
    """A chunk of rows is a chunk of *entries*, and only entries are residency.

    The solver hand-off reads ``matrix`` a range at a time and holds that range
    while it works it. Sizing the range in rows bounds the wrong quantity: the
    same 100k-row range is 900k entries in ``transport`` and 10M in
    ``dispatch``, so what the sink actually holds is set by the model's shape
    rather than by the budget — which defeats the point of batching at all: a
    pass that holds a slice proportional to the model is a pass that holds the
    model.

    Wide rows are the case that separates the two, so this builds them: 50
    generators summed into each of 4 snapshots is 50 entries per row.
    """
    n_g, n_s = 50, 4
    gens = pd.DataFrame(
        {
            'generator': [f'g{i}' for i in range(n_g)],
            'p_max': np.full(n_g, 10.0),
            'cost': np.arange(1.0, n_g + 1.0),
        }
    )
    load = pd.DataFrame({'snapshot': np.arange(n_s), 'value': np.full(n_s, 100.0)})

    with PolarsExecutor() as ex:
        ex.build(dispatch_program(), dispatch_sources(gens, load))
        tables = ex._tables()
        assert tables.matrix.height == n_g * n_s

        def widest(ranges):
            return max(
                tables.matrix.filter(pl.col('row').is_between(lo, hi, closed='left')).height for lo, hi in ranges
            )

        budget = 100
        assert widest(tables.row_chunks_by_nonzeros(budget)) <= budget

        # the same budget spent as if a row cost one element puts every entry
        # in one chunk — 2x the budget here, and unbounded in general, because
        # nothing caps how wide a row gets
        assert widest(chunking.ranges(tables.row_count, budget, 1.0)) == n_g * n_s


@pytest.mark.parametrize(
    ('total', 'budget', 'width'),
    [(0, 100, 1.0), (1, 100, 1.0), (10, 3, 1.0), (10, 100, 1.0), (10, 3, 7.0), (10, 3, 0.25), (10, 1, 1e9)],
)
def test_chunk_ranges_are_contiguous_gapless_and_cover_everything(total, budget, width):
    """The property the whole hand-off rests on, at every awkward size.

    ``addCols`` and ``addRows`` append: column *k* must be the *k*-th row handed
    over. That holds only if the ranges are ordered, consecutive and gapless —
    a dropped range silently shortens the model, an overlapping one relabels
    it, and neither shows up as an error. The widths here include one below 1
    and one far above the budget, the two ends where a ``//`` can produce a
    zero step or a chunk wider than asked for.
    """
    got = list(chunking.ranges(total, budget, width))

    assert all(lo < hi for lo, hi in got), 'an empty range means a wasted pass'
    assert [lo for lo, _ in got] == sorted(lo for lo, _ in got), 'ranges must ascend'
    assert [i for lo, hi in got for i in range(lo, hi)] == list(range(total)), 'gap, overlap, or short'
    if total:
        widest = max(hi - lo for lo, hi in got)
        assert widest * max(1.0, width) <= max(budget, max(1.0, width)), 'a chunk exceeded the budget'


SPARSE_COEFFICIENT_MODEL = {
    'dimensions': {'t': {'dtype': 'int', 'values': [0, 1, 2]}},
    'parameters': {'c': {'dims': ['t']}, 'w': {'dims': ['t']}},
    'variables': {'x': {'foreach': ['t'], 'bounds': {'lower': 0, 'upper': 10}}},
    'constraints': {'cap': {'foreach': ['t'], 'expression': 'w * x <= c'}},
    'objectives': {'o': {'sense': 'maximize', 'expression': 'sum(x, over=t)'}},
}


def test_a_parameter_covering_a_subset_of_its_dims_means_zero_on_both_lanes():
    """A tidy parameter table is a compressed dense array, not a record of absence.

    Supplying rows only where a coefficient is nonzero is the language's own
    sparsity idiom (SPEC §8, "sparse data gives sparse variables"), and the
    relational lane has always read an uncovered coordinate as zero — the
    constant fragments are left-joined and filled. The eager lane got the same
    answer only by accident of legacy linopy's implicit NaN fill; under the v1
    convention a NaN in a user constant is refused outright (§5), because from
    inside linopy a deliberate absence and a data error look identical.

    The disambiguation is positional and only this side knows it, so it is made
    at the use site: zero in a coefficient, still-NaN in ``bounds:`` (where it
    raises, since unbounded is not bounded-at-zero) and in a ``where`` (where it
    reads false, which is what §6's bare name means).

    Both lanes are asserted here rather than the fill alone: the point is not
    that we fill, it is that the two agree about what a missing row meant.
    """
    data = {
        # no row at t=0 for either: the coefficient and the bound are both sparse
        'w': pd.Series({1: 1.0, 2: 1.0}),
        'c': pd.Series({1: 4.0, 2: 5.0}),
    }
    with differential(SPARSE_COEFFICIENT_MODEL, data, lp=True) as run:
        # t=0 carries `0 * x <= 0` — a row that exists and constrains nothing,
        # which is what a zero coefficient and a zero right-hand side mean.
        assert run.result.objective == pytest.approx(10.0 + 4.0 + 5.0, rel=RTOL)


ABSENT_VARIABLE_MODEL = {
    'dimensions': {'f': {'values': ['a', 'b']}},
    'parameters': {'gate': {'dims': ['f'], 'dtype': 'bool'}, 'relmax': {'dims': ['f']}, 'cost': {'dims': ['f']}},
    'variables': {
        'x': {'foreach': ['f'], 'bounds': {'lower': 0, 'upper': 100}},
        'size': {'foreach': ['f'], 'where': 'gate', 'bounds': {'lower': 0, 'upper': 50}},
    },
    'constraints': {'envelope': {'foreach': ['f'], 'expression': 'x - relmax * size <= 0'}},
    'objectives': {'total': {'sense': 'maximize', 'expression': 'sum(x * cost, over=f)'}},
}


def test_a_term_whose_variable_is_absent_drops_the_row_on_both_lanes():
    """Absence propagates into the comparison; it does not zero the term.

    ``x - relmax * size <= 0`` where ``size`` is masked out used to build
    ``x <= 0`` — a row that silently pinned the flow to zero. Plausible answer,
    no error, which is goal 1 of linopy's v1 convention ("no silent wrong
    answers") and the whole of PyPSA/linopy#712. Under §6 the slot is absent and
    §12 drops the row instead, so ``x`` is left free at ``f=b`` and bounded only
    by its own declaration.

    The oracle is the point: the eager lane gets this from linopy's own v1
    semantics, the relational lane from carrying variable presence apart from
    the term stream. Two independent implementations, one answer.
    """
    data = {
        'gate': pd.Series({'a': True}),
        'relmax': pd.Series({'a': 0.5, 'b': 0.5}),
        'cost': pd.Series({'a': 1.0, 'b': 1.0}),
    }
    with differential(ABSENT_VARIABLE_MODEL, data, lp=True) as run:
        x = by_coord(run.result, 'x', 'f')
        assert x['a'] == pytest.approx(25.0, rel=RTOL), 'sized: x <= 0.5 * size, size <= 50'
        assert x['b'] == pytest.approx(100.0, rel=RTOL), 'unsized: the row is gone, so only the bound holds'


DEFINED_MODEL = {
    'dimensions': {'f': {'values': ['a', 'b']}},
    'parameters': {'gate': {'dims': ['f'], 'dtype': 'bool'}, 'relmax': {'dims': ['f']}, 'cost': {'dims': ['f']}},
    'variables': {
        'x': {'foreach': ['f'], 'bounds': {'lower': 0, 'upper': 100}},
        'size': {'foreach': ['f'], 'where': 'gate', 'bounds': {'lower': 0, 'upper': 50}},
    },
    'constraints': {
        # one rule per block, so the two regimes are two named constraints
        'envelope_sized': {'foreach': ['f'], 'where': 'size', 'expression': 'x - relmax * size <= 0'},
        'envelope_unsized': {'foreach': ['f'], 'where': 'NOT size', 'expression': 'x <= 0'},
    },
    'objectives': {'total': {'sense': 'maximize', 'expression': 'sum(x * cost, over=f)'}},
}


def test_a_bare_variable_name_in_a_where_asks_whether_it_exists():
    """The escape hatch for a language where absence drops the row.

    Since a term whose variable is absent takes the row with it, a model that
    wanted the *other* reading — keep the row, treat the term as zero — needs a
    way to say which coordinates those are. A bare parameter name in a ``where``
    already asks "does this have a value here"; a bare variable name asks "does
    this exist here", and the two complementary clauses spell out both cases.

    Without it the only way to write this is a parameter mirroring the
    variable's own mask, which is two sources for one fact and drifts.
    """
    data = {
        'gate': pd.Series({'a': True}),
        'relmax': pd.Series({'a': 0.5, 'b': 0.5}),
        'cost': pd.Series({'a': 1.0, 'b': 1.0}),
    }
    with differential(DEFINED_MODEL, data, lp=True) as run:
        x = by_coord(run.result, 'x', 'f')
        assert x['a'] == pytest.approx(25.0, rel=RTOL), 'sized: the envelope binds'
        assert x['b'] == pytest.approx(0.0, abs=1e-9), 'unsized: the complementary clause pins it'


ABSENT_COEFFICIENT_MODEL = {
    'dimensions': {'f': {'values': ['a', 'b']}},
    'parameters': {'relmax': {'dims': ['f']}, 'cost': {'dims': ['f']}},
    'variables': {
        'x': {'foreach': ['f'], 'bounds': {'lower': 0, 'upper': 100}},
        'size': {'foreach': ['f'], 'bounds': {'lower': 0, 'upper': 50}},
    },
    'constraints': {'envelope': {'foreach': ['f'], 'expression': 'x - relmax * size <= 0'}},
    'objectives': {'total': {'sense': 'maximize', 'expression': 'sum(x * cost, over=f)'}},
}


def test_a_sparse_coefficient_on_the_bound_side_still_pins_the_variable():
    """The half of §6's hazard that survives absence propagation.

    Same expression as ``ABSENT_VARIABLE_MODEL`` above, one operand different:
    the thing missing at ``f=b`` is the *parameter* ``relmax``, not the variable
    ``size``. Absence is a property of variables, so nothing propagates — the
    row is kept, the term is dropped, and ``x <= 0`` is built.

    That is correct and it is the documented reading of a sparse coefficient
    table, but it is the same silently-wrong shape the v1 convention removed
    from the variable side, so SPEC §6 now names it and this pins the behaviour
    the prose describes. The benign case is
    ``test_a_parameter_covering_a_subset_of_its_dims_means_zero_on_both_lanes``:
    there the zero lands on a coefficient *and* a right-hand side, so the row
    constrains nothing. Here the right-hand side is a literal 0 and the missing
    coefficient was the whole bound.
    """
    data = {
        'relmax': pd.Series({'a': 0.5}),  # no row at 'b'
        'cost': pd.Series({'a': 1.0, 'b': 1.0}),
    }
    with differential(ABSENT_COEFFICIENT_MODEL, data, lp=True) as run:
        x = by_coord(run.result, 'x', 'f')
        assert x['a'] == pytest.approx(25.0, rel=RTOL), 'sized: x <= 0.5 * size, size <= 50'
        assert x['b'] == pytest.approx(0.0, abs=1e-9), 'the row survived the missing coefficient and pins x'


def _reindexed_parameter_model(op: str) -> dict:
    return {
        'dimensions': {'t': {'dtype': 'int', 'values': [0, 1, 2]}},
        'parameters': {'dt': {'dims': ['t']}},
        'variables': {'x': {'foreach': ['t'], 'bounds': {'lower': 0, 'upper': 100}}},
        'constraints': {'r': {'foreach': ['t'], 'expression': f'x <= {op}'}},
        'objectives': {'o': {'sense': 'maximize', 'expression': 'sum(x, over=t)'}},
    }


@pytest.mark.parametrize(
    ('op', 'expected'),
    [
        # roll is cyclic: nothing is vacated, so t=0 reads the last value
        ('shift(dt, over=t, by=1, edge=wrap)', {0: 7.0, 1: 5.0, 2: 6.0}),
        # shift with the escape hatch: the vacated position contributes zero,
        # and a zero in right-hand-side position is a pin
        ('shift(dt, over=t, by=1, edge=0)', {0: 0.0, 1: 5.0, 2: 6.0}),
    ],
)
def test_roll_and_filled_shift_re_index_a_parameter_not_only_a_variable(op, expected):
    """``array`` in §7 is any node, so these operators read a parameter.

    Worth its own test because every example in SPEC took a variable, and a
    downstream consumer built and shipped a hand-shifted copy of a parameter
    table before probing revealed this works.

    ``fill=0`` is what a *bare* ``shift`` used to mean here, and the pin it
    produces at ``t=0`` is why it stopped being the default — see the refusal
    below. Spelled out, it is a legitimate thing to ask for, so it still works.
    """
    data = {'dt': pd.Series({0: 5.0, 1: 6.0, 2: 7.0})}
    with differential(_reindexed_parameter_model(op), data, lp=True) as run:
        x = by_coord(run.result, 'x', 't')
        for t, want in expected.items():
            assert x[t] == pytest.approx(want, abs=1e-9), f'{op} at t={t}'


def test_a_bare_shift_over_data_is_refused_rather_than_filled():
    """The pin, removed at its source (#289).

    ``x <= shift(dt, over=t, by=1)`` used to build ``x <= 0`` at the first coordinate:
    a bound invented from a slot that has no value. Absence would be the
    consistent answer, but a parameter has no absence to propagate — a missing
    row is a zero coefficient (§6) — so this follows linopy v1 and refuses,
    at load time, naming the three things the author might have meant.

    Decidable without data, so ``lps.check()`` catches it: the operand is
    variable-free by declaration, not by what arrives in ``sources``.
    """
    model = _reindexed_parameter_model('shift(dt, over=t, by=1)')
    with pytest.raises(LanguageError) as exc:
        lps.check(model)
    assert 'edge=0' in str(exc.value), 'the refusal must name the escape hatch'
    assert 'edge=wrap' in str(exc.value), 'and the policy for a genuinely cyclic horizon'


PINNED_MODEL = {
    'dimensions': {'f': {'values': ['fixed', 'sized']}},
    'parameters': {'relmax': {'dims': ['f']}, 'size_lb': {'dims': ['f']}, 'size_ub': {'dims': ['f']}},
    'variables': {
        'rate': {'foreach': ['f'], 'bounds': {'lower': 0, 'upper': 1000}},
        'size': {'foreach': ['f'], 'bounds': {'lower': 'size_lb', 'upper': 'size_ub'}},
    },
    'constraints': {'envelope': {'foreach': ['f'], 'expression': 'rate - relmax * size <= 0'}},
    'objectives': {'total': {'sense': 'maximize', 'expression': 'sum(rate, over=f)'}},
}


def test_equal_bounds_pin_a_variable_so_one_equation_covers_both_regimes():
    """A capacity that is data in one model and a decision in another.

    The alternative a consumer reaches for otherwise is a block per regime with
    pre-multiplied coefficients — ``rate_max_at_size``, ``rate_max_when_on`` —
    whose names encode which regime they belong to rather than what quantity
    they are. Pinning with equal bounds writes the row form once and lets
    presolve substitute the fixed column, so SPEC §2 documents it and this shows
    both regimes coming out of the single equation.
    """
    data = {
        'relmax': pd.Series({'fixed': 0.8, 'sized': 0.8}),
        'size_lb': pd.Series({'fixed': 10.0, 'sized': 0.0}),
        'size_ub': pd.Series({'fixed': 10.0, 'sized': 50.0}),
    }
    with differential(PINNED_MODEL, data, lp=True) as run:
        rate = by_coord(run.result, 'rate', 'f')
        assert rate['fixed'] == pytest.approx(8.0, rel=RTOL), 'pinned at 10, so the envelope is 0.8 * 10'
        assert rate['sized'] == pytest.approx(40.0, rel=RTOL), 'free to 50, so the envelope is 0.8 * 50'


SCALAR_MASKED_MODEL = {
    'dimensions': {'f': {'values': ['a', 'b']}},
    'parameters': {'cost': {'dims': ['f']}, 'budget': {'dims': []}},
    'variables': {
        'x': {'foreach': ['f'], 'bounds': {'lower': 0, 'upper': 100}},
        'slack': {'foreach': [], 'where': 'budget > 100', 'bounds': {'lower': 0, 'upper': 10}},
    },
    'constraints': {'cap': {'foreach': [], 'expression': 'sum(x, over=f) - slack <= budget'}},
    'objectives': {'total': {'sense': 'maximize', 'expression': 'x * cost'}},
}


def test_a_scalar_variable_may_not_be_masked():
    """The one case `foreach: []` on a variable does not cover (#340).

    Presence is `select()` over no dims, and polars cannot hold one row of no
    columns — collecting reports (0, 0), so present and absent are the same
    frame. The restriction is lost and `cap` stays enforced with the term gone:
    340.0 where the eager lane, and law 7, say 600.0. The dimensional case has
    a column to key on, which is what makes this the exception.

    `check()` catches it, so a model repository in CI does not need data bound
    to see it.
    """
    with pytest.raises(ValueError) as exc:
        lps.check(SCALAR_MASKED_MODEL)

    assert '#340' in str(exc.value), 'the refusal must name where the fix is tracked'
    assert 'dimension of size 1' in str(exc.value), 'and the workaround'


#: A masked variable broadcast onto a wider frame, then reduced back. `p` is
#: over (node, tech); `produces` adds `carrier`; the sum removes `tech`. So the
#: constraint's dims are neither a subset nor a superset of the variable's.
BROADCAST_MASK_MODEL = {
    'dimensions': {
        'node': {'values': ['n1', 'n2']},
        'tech': {'values': ['t1', 't2']},
        'carrier': {'values': ['elec', 'heat']},
    },
    'parameters': {
        'produces': {'dims': ['tech', 'carrier']},
        'demand': {'dims': ['node', 'carrier']},
        'cost': {'dims': ['tech']},
        'installed': {'dims': ['node', 'tech']},
    },
    'variables': {
        'p': {'foreach': ['node', 'tech'], 'where': 'installed > 0', 'bounds': {'lower': 0, 'upper': 'installed'}},
    },
    'constraints': {
        'balance': {'foreach': ['node', 'carrier'], 'expression': 'sum(p * produces, over=tech) == demand'},
    },
    'objectives': {'total': {'sense': 'minimize', 'expression': 'p * cost'}},
}


def test_a_mask_survives_a_broadcast_into_a_reduction():
    """`presence_dims=None` means "keyed by dims", and a product may *widen*
    dims — so carrying it through the widening re-read `p`'s (node, tech)
    presence as keyed by (node, tech, carrier) and `_propagate_absence` selected
    a column it never had (#345).

    Unmasked the same model was fine, which is what made it look like a problem
    with the coordinate dim rather than with the mask. The whole benchmark
    `sector` case sat on this.
    """
    import pandas as pd
    import xarray as xr

    data = {
        # a tech produces exactly one carrier, which is what makes `produces` sparse
        'produces': xr.DataArray(
            [[1.0, 0.0], [0.0, 1.0]],
            coords={'tech': ['t1', 't2'], 'carrier': ['elec', 'heat']},
            dims=['tech', 'carrier'],
        ),
        'demand': xr.DataArray(
            [[10.0, 20.0], [10.0, 20.0]],
            coords={'node': ['n1', 'n2'], 'carrier': ['elec', 'heat']},
            dims=['node', 'carrier'],
        ),
        'cost': pd.Series({'t1': 1.0, 't2': 2.0}),
        'installed': xr.DataArray(
            [[100.0, 100.0], [100.0, 100.0]], coords={'node': ['n1', 'n2'], 'tech': ['t1', 't2']}, dims=['node', 'tech']
        ),
    }

    with differential(BROADCAST_MASK_MODEL, data) as run:
        assert run.result.objective == pytest.approx(100.0, rel=RTOL)


LABEL_MODEL = {
    'dimensions': {'f': {'values': ['a', 'b']}},
    'parameters': {'cost': {'dims': ['f']}, 'cap': {'dims': ['f']}},
    'variables': {'x': {'foreach': ['f'], 'bounds': {'lower': 0, 'upper': 'cap'}}},
    'constraints': {'k': {'foreach': ['f'], 'expression': 'x <= cap'}},
    'objectives': {'o': {'sense': 'maximize', 'expression': 'x * cost'}},
}
_CAP = pl.DataFrame({'f': ['a', 'b'], 'value': [5.0, 5.0]})


def test_a_label_the_dimension_does_not_have_is_refused():
    """A typo used to be worth two thirds of the objective (#350).

    `b` mistyped as `zz` left `b` with no cost row, which reads as a zero
    coefficient — so the model solved, reported optimal, and returned 5.0 where
    15.0 is right. The eager lane already refused it; this lane joined the
    stray row against nothing and carried on.
    """
    ok = {'cost': pl.DataFrame({'f': ['a', 'b'], 'value': [1.0, 2.0]}), 'cap': _CAP}
    assert lps.solve(LABEL_MODEL, ok).objective == pytest.approx(15.0)

    typo = {'cost': pl.DataFrame({'f': ['a', 'zz'], 'value': [1.0, 2.0]}), 'cap': _CAP}
    with pytest.raises(DataError) as exc:
        lps.solve(LABEL_MODEL, typo)
    assert "'zz'" in str(exc.value), 'the refusal must name the offending label'
    assert 'typo' in str(exc.value)


def test_a_missing_row_is_still_only_sparse():
    """The distinction the refusal above rests on. A row that is *absent* is
    ordinary — it reads as a zero coefficient (§8) — and only a row that is
    present and unaddressable is a typo. Refusing both would make sparsity,
    which is the common case, an error.
    """
    sparse = {'cost': pl.DataFrame({'f': ['a'], 'value': [1.0]}), 'cap': _CAP}
    assert lps.solve(LABEL_MODEL, sparse).objective == pytest.approx(5.0)


def test_a_derived_dimension_cannot_have_a_stranger():
    """`values: null` takes the dimension's labels *from* the parameters, so the
    union of what arrived is the definition and the check has nothing to ask.
    Running it anyway would refuse every such model.
    """
    derived = {**LABEL_MODEL, 'dimensions': {'f': {'values': None}}}
    data = {'cost': pl.DataFrame({'f': ['a', 'b'], 'value': [1.0, 2.0]}), 'cap': _CAP}
    assert lps.solve(derived, data).objective == pytest.approx(15.0)


#: A `line` whose two endpoints are *both* multi-valued for one label — the case
#: that used to be reported one coordinate at a time.
TWO_BAD_COORDS_MODEL = {
    'dimensions': {'bus': {'values': ['b1', 'b2']}, 'line': {'coords': {'from': 'bus', 'to': 'bus'}}},
    'parameters': {'cap': {'dims': ['line']}},
    'variables': {'f': {'foreach': ['line'], 'bounds': {'lower': 0, 'upper': 'cap'}}},
    'constraints': {'k': {'foreach': ['line'], 'expression': 'f <= cap'}},
    'objectives': {'o': {'sense': 'maximize', 'expression': 'f'}},
}


def test_every_multi_valued_coordinate_is_named_at_once():
    """The per-coordinate loop this replaced raised on the first offender, so a
    source with two bad coordinates was fixed, rebuilt, and refused again (#273).

    Folding the counts into the caller's `group_by(d)` is what makes naming all
    of them free — they arrive in one frame instead of one pass each.
    """
    data = {
        'cap': pl.DataFrame({'line': ['l1'], 'value': [1.0]}),
        'bus': pl.DataFrame({'bus': ['b1', 'b2']}),
        'line': pl.DataFrame({'line': ['l1', 'l1'], 'from': ['b1', 'b2'], 'to': ['b2', 'b1']}),
    }

    with pytest.raises(DataError) as exc:
        lps.solve(TWO_BAD_COORDS_MODEL, data)

    message = str(exc.value)
    assert "'from'" in message and "'to'" in message, f'both offenders must be named; got: {message}'


def test_dense_columns_does_not_edit_the_model_it_projects():
    """Two solvers, two spellings of infinity, one model — and it survives both.

    `cols` has no `col` column: it is one row per column in label order, so the
    vectors a solver sink is handed are *views* of the frame rather than the
    scatter's fresh arrays. Replacing an infinity in place through one would
    rewrite the built model to suit whichever solver asked last, and the second
    ask would read bounds the first had already edited.
    """
    model = {
        'dimensions': {'i': {'dtype': 'int', 'values': [0, 1]}},
        'parameters': {'rhs': {'dims': ['i']}},
        'variables': {'x': {'foreach': ['i'], 'bounds': {'lower': 0}}},  # no upper: +inf
        'constraints': {'c': {'foreach': ['i'], 'expression': 'x >= rhs'}},
        'objectives': {'o': {'sense': 'minimize', 'expression': 'sum(x, over=i)'}},
    }
    with lps.build(model, {'rhs': pl.DataFrame({'i': [0, 1], 'value': [1.0, 2.0]})}) as ex:
        tables = ex._tables()
        first, _, _, _ = tables.dense_columns(1e30)
        ub_after_first = tables.cols['ub'].to_list()

        _, second_ub, _, _ = tables.dense_columns(1e100)

        assert ub_after_first == tables.cols['ub'].to_list(), 'the projection edited the model'
        assert all(v == float('inf') for v in tables.cols['ub'].to_list()), 'the frame lost its infinities'
        assert list(second_ub) == [1e100, 1e100], "the second solver got the first solver's infinity"
        assert list(first) == [0.0, 0.0]


@pytest.mark.parametrize('where', [None, 'cap > 0'])
def test_cols_is_positional_so_a_row_index_is_its_solver_column(where):
    """Every row of `cols` sits at its own label, masked or not.

    `cols` carries no `col`: it is one row per column in label order, so a
    row's position *is* the solver's index. Both arithmetic label paths get
    that order from the emission order of a **cross join**, which is a property
    of polars rather than of this package — so it is checked here against the
    label frame, which knows the answer independently.

    Two dims on purpose: one dim is a scan and cannot be out of order, so a
    single-dim model would pass whatever the product did. Bounds are distinct
    per coordinate for the same reason — a frame permuted by a mask, a join, or
    a change in someone else's engine still has the right *multiset* of bounds
    and the wrong row for every one of them.
    """
    caps = [
        {'i': i, 'j': j, 'value': 0.0 if where and (i, j) == (1, 'b') else float(10 * i + ord(j))}
        for i in range(4)
        for j in ('a', 'b', 'c')
    ]
    model = {
        'dimensions': {'i': {'dtype': 'int', 'values': list(range(4))}, 'j': {'values': ['a', 'b', 'c']}},
        'parameters': {'cap': {'dims': ['i', 'j']}},
        'variables': {'x': {'foreach': ['i', 'j'], 'bounds': {'lower': 0, 'upper': 'cap'}}},
        'constraints': {'c': {'foreach': ['i', 'j'], 'expression': 'x <= cap'}},
        'objectives': {'o': {'sense': 'maximize', 'expression': 'sum(sum(x, over=j), over=i)'}},
    }
    if where:
        model['variables']['x']['where'] = where

    with lps.build(model, {'cap': pl.DataFrame(caps)}) as ex:
        tables = ex._tables()
        assert 'col' not in tables.cols.columns, 'cols carries an index it does not need'
        assert tables.cols.height == tables.column_count

        labels = ex._variables['x'].collect().sort('var_label')
        expected = labels.join(pl.DataFrame(caps), on=['i', 'j'], how='left')['value'].to_list()
        assert tables.cols['ub'].to_list() == expected, 'a bound is attached to the wrong column'
