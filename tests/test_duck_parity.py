"""The duckdb port against the polars engine, frame for frame.

This is what makes `src/lpspec/relational/duck/` evidence rather than a sketch.
Both engines build the same `Program` from the same bound sources and must
produce the same four `ModelTables` frames — exactly, not approximately:
`col` and `row` *are* the solver's own indices, so an off-by-one is a different
model that still solves.

The models are the real ones under `examples/` and `bench/models/`, reached
through the ordinary lowering, so nothing here is a fixture written to pass.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

import lpspec as lps
from lpspec.relational.duck import DuckExecutor

duckdb = pytest.importorskip('duckdb')

ROOT = Path(__file__).resolve().parent.parent


def _sorted(frame: pl.DataFrame, by: list[str]) -> pl.DataFrame:
    return frame.sort(by).select(sorted(frame.columns))


def _both(model: Path, sources: dict[str, object], coords: dict[str, object] | None = None):
    """The same program built twice, as (polars tables, duckdb tables).

    The public API does the lowering and the binding; the executor underneath
    it is the only thing swapped, which is the claim this port makes.
    """
    ex = lps.build(model, sources, coords=coords)
    duck = DuckExecutor()
    duck.build(ex._program, ex._bound)
    return ex._tables(), duck._tables()


DISPATCH = {
    'p_max': pl.DataFrame({'generator': ['wind', 'solar', 'gas'], 'value': [10.0, 5.0, 100.0]}),
    'cost': pl.DataFrame({'generator': ['wind', 'solar', 'gas'], 'value': [1.0, 2.0, 50.0]}),
    'load': pl.DataFrame({'snapshot': [0, 1, 2], 'value': [12.0, 8.0, 20.0]}),
}


CASES = [
    # `storage` is the one that exercises Translate: a cyclic `shift` in the
    # SOC recurrence, which is the operator §3 of the spike calls the hardest
    # to re-derive rather than translate.
    ('examples/storage.yaml', DISPATCH, None),
    ('examples/dispatch.yaml', DISPATCH, None),
]


def _bench_case(name: str):
    """One bench case at its smallest rung, as (model, sources, coords).

    The bench models are where the operators that tax SQL actually appear —
    `nodal` and `sector` mask, `transport` group_sums three ways — so they are
    the parity set rather than fixtures written here.
    """
    import sys

    sys.path.insert(0, str(ROOT))
    import yaml as pyyaml

    from bench.cases import CASES as BENCH

    case = BENCH[name]
    paths = case.data(case.ladder[0], cache=ROOT / 'bench' / '.cache')
    schema = pyyaml.safe_load(case.model.read_text())
    params, dims = set(schema.get('parameters', {})), set(schema.get('dimensions', {}))
    return (
        case.model,
        {k: v for k, v in paths.items() if k in params},
        {k: v for k, v in paths.items() if k in dims},
    )


BENCH_CASES = ['dispatch', 'fleet', 'nodal', 'profiled', 'sector', 'transport']


@pytest.mark.parametrize('name', BENCH_CASES)
def test_the_bench_models_build_identically(name):
    model, sources, coords = _bench_case(name)
    polars_tables, duck_tables = _both(model, sources, coords)
    _compare(polars_tables, duck_tables)


@pytest.mark.parametrize(('model', 'sources', 'coords'), CASES)
def test_the_two_engines_build_the_same_model(model, sources, coords):
    polars_tables, duck_tables = _both(ROOT / model, sources, coords)
    _compare(polars_tables, duck_tables)


def _compare(polars_tables, duck_tables) -> None:
    """The four frames, exactly — an off-by-one label is a different model."""
    assert duck_tables.column_count == polars_tables.column_count
    assert duck_tables.row_count == polars_tables.row_count
    assert duck_tables.objective_sense == polars_tables.objective_sense
    assert duck_tables.objective_constant == pytest.approx(polars_tables.objective_constant)

    for name, keys in (('cols', ['col']), ('rows', ['row']), ('matrix', ['row', 'col']), ('obj', ['col'])):
        want = _sorted(getattr(polars_tables, name), keys)
        got = _sorted(getattr(duck_tables, name), keys)
        assert got.height == want.height, f'{name}: {got.height} rows against {want.height}'
        assert want.equals(got), f'{name} differs:\n{want.head(10)}\n{got.head(10)}'
