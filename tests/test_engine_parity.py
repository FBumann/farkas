"""The two engines, on the same YAML, must answer the same.

`--engine duckdb` already runs the whole suite on the other engine, which is
the broad check. This is the narrow one: both engines in **one process**, on
the same model, compared outright. It catches what a full-suite run cannot —
a difference that is stable, so both runs pass their own assertions while
disagreeing with each other.

What is compared is what a caller can observe: the objective, the primal, the
duals, and the LP file byte for byte. Not the four frames — those are checked
here too, but through `_tables()`, because `row` and `col` *are* the solver's
own indices and an off-by-one there is a different model that still solves.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

import lpspec as lps

pytest.importorskip('duckdb')

ROOT = Path(__file__).resolve().parent.parent
ENGINES = ('polars', 'duckdb')

DISPATCH = {
    'p_max': pl.DataFrame({'generator': ['wind', 'solar', 'gas'], 'value': [10.0, 5.0, 100.0]}),
    'cost': pl.DataFrame({'generator': ['wind', 'solar', 'gas'], 'value': [1.0, 2.0, 50.0]}),
    'load': pl.DataFrame({'snapshot': [0, 1, 2], 'value': [12.0, 8.0, 20.0]}),
}

#: `storage` carries a cyclic `shift`, `transport` three `group_sum`s — the two
#: operators a second engine is most likely to get subtly wrong.
MODELS = [
    ('examples/dispatch.yaml', DISPATCH),
    ('examples/storage.yaml', DISPATCH),
]


def _frames(tables) -> dict[str, pl.DataFrame]:
    return {
        'cols': tables.cols.sort('col'),
        'rows': tables.rows.sort('row'),
        'matrix': tables.matrix.sort('row', 'col'),
        'obj': tables.obj.sort('col'),
    }


@pytest.mark.parametrize(('model', 'sources'), MODELS)
def test_both_engines_build_the_same_model(model, sources):
    built = {}
    try:
        for name in ENGINES:
            ex = lps.build(ROOT / model, sources, engine=name)
            built[name] = (ex, ex._tables())

        left, right = (_frames(t) for _, t in (built['polars'], built['duckdb']))
        for frame in ('cols', 'rows', 'matrix', 'obj'):
            assert left[frame].equals(right[frame]), f'{frame} differs between engines'

        (_, a), (_, b) = built['polars'], built['duckdb']
        assert a.column_count == b.column_count
        assert a.row_count == b.row_count
        assert a.objective_sense == b.objective_sense
        assert a.objective_constant == pytest.approx(b.objective_constant)
    finally:
        for ex, _ in built.values():
            ex.close()


@pytest.mark.parametrize(('model', 'sources'), MODELS)
def test_both_engines_solve_to_the_same_answer(model, sources):
    answers = {}
    for name in ENGINES:
        with lps.solve(ROOT / model, sources, engine=name) as result:
            assert result.is_ok
            answers[name] = (
                result.objective,
                result.primal('p').sort(result.primal('p').columns),
                result.dual('power_balance').sort('snapshot'),
            )

    (obj_a, primal_a, dual_a), (obj_b, primal_b, dual_b) = answers['polars'], answers['duckdb']
    assert obj_a == pytest.approx(obj_b)
    assert primal_a.equals(primal_b), 'primals differ between engines'
    assert dual_a.equals(dual_b), 'duals differ between engines'


@pytest.mark.parametrize(('model', 'sources'), MODELS)
def test_both_engines_write_the_same_lp_file(model, sources, tmp_path):
    written = {}
    for name in ENGINES:
        out = lps.write(ROOT / model, sources, tmp_path / f'{name}.lp', engine=name)
        written[name] = out.read_bytes()
    assert written['polars'] == written['duckdb'], 'the LP files differ byte for byte'


def test_the_engine_option_is_not_silently_a_no_op(pytestconfig):
    """`--engine X` must actually build on X, and this must fail if it stops.

    Everything the suite claims about a second engine rests on having *run* on
    it. If `resolve` ever read the default at import time instead of call time,
    or the session fixture stopped being applied, every test would still pass
    and the claim would quietly become vacuous — a green suite proving nothing.
    So the switch is asserted outright, in whichever mode the run is in.
    """
    expected = {
        'polars': 'PolarsExecutor',
        'duckdb': 'DuckExecutor',
        None: 'PolarsExecutor',
    }[pytestconfig.getoption('--engine')]
    with lps.build(ROOT / 'examples/dispatch.yaml', DISPATCH) as ex:
        assert type(ex).__name__ == expected


def test_the_env_var_sets_the_engine_and_an_explicit_argument_beats_it(monkeypatch):
    """`LPSPEC_ENGINE` is the process-wide switch; `engine=` still wins.

    The precedence is the whole design: an environment can say "try the other
    engine everywhere" without a code change, and a call that has already made
    up its mind is not overridden by the shell it happens to run in.
    """
    from lpspec.relational import engines

    monkeypatch.setenv(engines.ENV_VAR, 'duckdb')
    with lps.build(ROOT / 'examples/dispatch.yaml', DISPATCH) as ex:
        assert type(ex).__name__ == 'DuckExecutor'
    with lps.build(ROOT / 'examples/dispatch.yaml', DISPATCH, engine='polars') as ex:
        assert type(ex).__name__ == 'PolarsExecutor'


def test_a_typo_in_the_env_var_says_where_it_came_from(monkeypatch):
    """Otherwise an unknown name in a shell profile reads as a library bug."""
    from lpspec.relational import engines

    monkeypatch.setenv(engines.ENV_VAR, 'ducdkb')
    with pytest.raises(ValueError, match=r"unknown engine 'ducdkb' \(from LPSPEC_ENGINE\)"):
        lps.build(ROOT / 'examples/dispatch.yaml', DISPATCH)


def test_an_unknown_engine_names_the_ones_that_exist():
    with pytest.raises(ValueError, match=r"unknown engine 'nope' — available: 'polars', 'duckdb'"):
        lps.build(ROOT / 'examples/dispatch.yaml', DISPATCH, engine='nope')


def test_an_engine_class_is_accepted_directly():
    """A caller may hand over a class, so trying an engine needs no registry entry."""
    from lpspec.relational.engines.duck import DuckExecutor

    with lps.build(ROOT / 'examples/dispatch.yaml', DISPATCH, engine=DuckExecutor) as ex:
        assert isinstance(ex, DuckExecutor)
        assert ex._tables().column_count == 9
