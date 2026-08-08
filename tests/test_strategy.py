"""The fold: one plan per slice, and the answers stitched back together.

What is checked here is that the driver is a *driver* — every claim below is
about slicing, coupling and folding, and none of it is about the language. The
windowed model in ``WINDOW_YAML`` uses only constructs that already ship, which
is the whole argument for building this above ``api.py`` rather than inside it.
"""

from __future__ import annotations

import datetime
import multiprocessing
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor

import polars as pl
import pytest

import lpspec as lps
from lpspec import strategy

# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

#: Dispatch, with a scenario-free declaration — the slice column never appears
#: in the model, which is what lets `EachCoordinate` need no language support.
DISPATCH = {
    'dimensions': {'snapshot': {'dtype': 'int'}, 'generator': {'dtype': 'str'}},
    'parameters': {
        'p_max': {'dims': ['generator']},
        'cost': {'dims': ['generator']},
        'load': {'dims': ['snapshot']},
    },
    'variables': {'p': {'foreach': ['snapshot', 'generator'], 'bounds': {'lower': 0, 'upper': 'p_max'}}},
    'constraints': {'balance': {'foreach': ['snapshot'], 'expression': 'sum(p, over=generator) == load'}},
    'objectives': {'total': {'sense': 'minimize', 'expression': 'sum(p * cost, over=generator)'}},
}

#: Storage over a *local* index, with the seam split out by a `where` on a dim
#: literal. `soc_step` carries no `edge=`, so its vacated row drops and the
#: masked `soc_open` supplies it from a carried parameter.
WINDOW = {
    'dimensions': {'t': {'dtype': 'int'}, 'generator': {'dtype': 'str'}},
    'parameters': {
        'p_max': {'dims': ['generator']},
        'cost': {'dims': ['generator']},
        'load': {'dims': ['t']},
        'soc_initial': {'dims': []},
    },
    'variables': {
        'p': {'foreach': ['t', 'generator'], 'bounds': {'lower': 0, 'upper': 'p_max'}},
        'charge': {'foreach': ['t'], 'bounds': {'lower': 0, 'upper': 30}},
        'discharge': {'foreach': ['t'], 'bounds': {'lower': 0, 'upper': 30}},
        'soc': {'foreach': ['t'], 'bounds': {'lower': 0, 'upper': 100}},
    },
    'constraints': {
        'balance': {
            'foreach': ['t'],
            'expression': 'sum(p, over=generator) + discharge - charge == load',
        },
        'soc_open': {
            'foreach': ['t'],
            'where': 't == 0',
            'expression': 'soc == soc_initial + charge * 0.9 - discharge',
        },
        'soc_step': {
            'foreach': ['t'],
            'where': 't > 0',
            'expression': 'soc == shift(soc, over=t, by=1) + charge * 0.9 - discharge',
        },
    },
    'objectives': {'total': {'sense': 'minimize', 'expression': 'sum(p * cost, over=generator)'}},
}

GENERATORS = ['wind', 'gas']
STATIC = {
    'p_max': pl.DataFrame({'generator': GENERATORS, 'value': [10.0, 100.0]}),
    'cost': pl.DataFrame({'generator': GENERATORS, 'value': [1.0, 50.0]}),
}


def scenario_sources() -> dict[str, object]:
    """Three scenarios differing only in load — `load` carries the slice key."""
    rows = []
    for scenario, scale in (('low', 1.0), ('mid', 2.0), ('high', 3.0)):
        rows += [{'scenario': scenario, 'snapshot': t, 'value': 5.0 * scale + t} for t in range(4)]
    return {**STATIC, 'load': pl.DataFrame(rows)}


def horizon_sources(periods: int = 12) -> dict[str, object]:
    load = [5.0, 9.0, 30.0, 40.0, 6.0, 8.0, 35.0, 45.0, 7.0, 10.0, 25.0, 50.0]
    return {
        **STATIC,
        'load': pl.DataFrame({'snapshot': range(periods), 'value': load[:periods]}),
        'soc_initial': pl.DataFrame({'value': [0.0]}),
    }


# ---------------------------------------------------------------------------
# EachCoordinate — the independent case
# ---------------------------------------------------------------------------


def test_a_scenario_sweep_solves_each_slice_and_keys_the_answers():
    """The model never mentions `scenario`; the driver filters and drops it.

    That is the whole reason this needs no language change — a slice is the
    same declaration bound to a narrower source.
    """
    runs = lps.solve_over(DISPATCH, scenario_sources(), lps.EachCoordinate('scenario'), keep=('p',))

    assert len(runs) == 3
    assert runs.keys == ['high', 'low', 'mid']  # sorted, not data order
    assert runs.objective.columns == ['scenario', 'status', 'termination_condition', 'objective']
    assert set(runs.primal('p').columns) == {'scenario', 'snapshot', 'generator', 'value'}
    assert runs.primal('p').height == 3 * 4 * 2

    # a bigger load is a costlier dispatch, monotonically
    by_key = dict(zip(runs.objective['scenario'], runs.objective['objective'], strict=True))
    assert by_key['low'] < by_key['mid'] < by_key['high']


def test_each_slice_matches_solving_that_slice_alone():
    """The fold must not change the answer — the oracle is `solve` itself."""
    runs = lps.solve_over(DISPATCH, scenario_sources(), lps.EachCoordinate('scenario'))
    folded = dict(zip(runs.objective['scenario'], runs.objective['objective'], strict=True))

    for scenario, expected in folded.items():
        one = scenario_sources()
        one['load'] = one['load'].filter(pl.col('scenario') == scenario).drop('scenario')
        with lps.solve(DISPATCH, one) as result:
            assert result.objective == pytest.approx(expected)


def test_an_axis_naming_a_column_no_source_carries_says_so():
    with pytest.raises(lps.DataError, match="no source carries a 'draw' column"):
        lps.solve_over(DISPATCH, scenario_sources(), lps.EachCoordinate('draw'))


def test_a_variable_that_was_not_kept_names_the_flag():
    """A fold releases each model as it goes, so this cannot be answered late."""
    runs = lps.solve_over(DISPATCH, scenario_sources(), lps.EachCoordinate('scenario'))
    with pytest.raises(lps.LpspecError, match=r'was not kept .* keep=\(\.\.\.\)'):
        runs.primal('p')


# ---------------------------------------------------------------------------
# EachWindow — the coupled case
# ---------------------------------------------------------------------------


def test_a_rolling_horizon_carries_state_across_the_seam():
    """Three contiguous windows, the store's level handed forward.

    `soc_initial` is rebound per window from the previous window's last `soc`,
    which is the carry doing its one job — a copy, at a named index.
    """
    runs = lps.solve_over(
        WINDOW,
        horizon_sources(),
        lps.EachWindow('snapshot', length=4, step=4, into='t'),
        carry={'soc_initial': ('soc', 3)},
        keep=('p', 'soc'),
    )

    assert runs.keys == [0, 4, 8]
    assert runs.primal('p').height == 3 * 4 * 2
    assert set(runs.primal('soc').columns) == {'snapshot', 't', 'value'}
    assert runs.objective['objective'].to_list() == pytest.approx([2270.0, 2770.0, 2655.0])


def test_overlapping_windows_advance_by_step_and_look_ahead_by_length():
    runs = lps.solve_over(
        WINDOW,
        horizon_sources(),
        lps.EachWindow('snapshot', length=6, step=3, into='t'),
        carry={'soc_initial': ('soc', 2)},  # the last *kept* row, not the last row
        keep=('soc',),
    )
    assert runs.keys == [0, 3, 6, 9]
    # the tail window is short rather than padded — 9..11 is three rows, not six
    assert runs.primal('soc').filter(pl.col('snapshot') == 9).height == 3


#: Six coordinates, three windows of two, whatever the coordinates *are*.
#:
#: `length` and `step` count coordinates rather than coordinate values, and
#: every row here is a case that measuring in values got wrong. Dense integers
#: from zero were the one shape that worked, because there value equals
#: position; spacing them by ten silently produced **26** mostly-empty slices,
#: and a datetime index raised `TypeError` from `int()`.
COORDINATE_TYPES = [
    pytest.param(list(range(6)), id='dense-ints'),
    pytest.param([0, 10, 20, 30, 40, 50], id='gapped-ints'),
    pytest.param(list(range(100, 106)), id='ints-not-from-zero'),
    pytest.param([datetime.datetime(2030, 1, 1, h) for h in range(6)], id='datetimes'),
    pytest.param([f's{i}' for i in range(6)], id='strings'),
]


@pytest.mark.parametrize('coordinates', COORDINATE_TYPES)
def test_a_window_spans_coordinates_whatever_they_are_numbered(coordinates):
    """The only requirement on a windowed dimension is that it is orderable.

    Not numeric, not dense, not starting anywhere in particular — and not time,
    which is only the common case. The local index is dense `0..n-1` by
    construction, which is also what keeps the seam's `where: "t == 0"`
    matching on a dimension with gaps in it.
    """
    sources = {
        **STATIC,
        'load': pl.DataFrame({'snapshot': coordinates, 'value': [5.0] * 6}),
        'soc_initial': pl.DataFrame({'value': [0.0]}),
    }
    runs = lps.solve_over(WINDOW, sources, lps.EachWindow('snapshot', length=2, step=2, into='t'), keep=('soc',))

    assert len(runs) == 3
    assert runs.keys == coordinates[::2], 'a window is keyed by its first coordinate'
    soc = runs.primal('soc')
    assert soc.height == 6
    assert sorted(soc['t'].unique().to_list()) == [0, 1], 'the local index is dense per window'


def test_the_window_geometry_is_checked_at_construction():
    """`__post_init__` is what earns these two a name on the public surface."""
    with pytest.raises(ValueError, match='exceeds length'):
        lps.EachWindow('snapshot', length=4, step=8, into='t')
    with pytest.raises(ValueError, match='must be positive'):
        lps.EachWindow('snapshot', length=0, step=1, into='t')
    with pytest.raises(ValueError, match='must differ from dim'):
        lps.EachWindow('snapshot', length=4, step=4, into='snapshot')
    with pytest.raises(ValueError, match='no default'):
        lps.EachWindow('snapshot', length=4, step=4, into='')


def test_a_carry_index_outside_the_slice_is_refused_by_name():
    with pytest.raises(lps.LpspecError, match='out of range'):
        lps.solve_over(
            WINDOW,
            horizon_sources(),
            lps.EachWindow('snapshot', length=4, step=4, into='t'),
            carry={'soc_initial': ('soc', 99)},
            keep=('soc',),
        )


def test_a_carry_reading_an_unkept_variable_points_at_keep():
    with pytest.raises(lps.LpspecError, match='which this run did not keep'):
        lps.solve_over(
            WINDOW,
            horizon_sources(),
            lps.EachWindow('snapshot', length=4, step=4, into='t'),
            carry={'soc_initial': ('soc', 3)},
            keep=(),
        )


# ---------------------------------------------------------------------------
# the fold's own rules
# ---------------------------------------------------------------------------


def test_carry_and_executor_are_refused_together():
    """Sequential by definition, so the combination is a call-time error rather
    than something discovered at slice two."""
    with pytest.raises(lps.LpspecError, match='mutually exclusive'):
        lps.solve_over(
            WINDOW,
            horizon_sources(),
            lps.EachWindow('snapshot', length=4, step=4, into='t'),
            carry={'soc_initial': ('soc', 3)},
            executor=object(),
        )


def test_carry_needs_an_ordered_axis():
    """Scenarios have no "next" slice for a value to move into."""
    with pytest.raises(lps.LpspecError, match='needs an ordered axis'):
        lps.solve_over(
            DISPATCH,
            scenario_sources(),
            lps.EachCoordinate('scenario'),
            carry={'p_max': ('p', 0)},
            keep=('p',),
        )


class Inline:
    """The whole protocol `solve_over` needs, in nine lines.

    Not a toy: it is the claim that ``executor=`` takes
    :class:`concurrent.futures.Executor` and not `ProcessPoolExecutor`, which
    is what lets a dask ``Client`` or any other pool plug in **without this
    package shipping a transport**. If the driver ever reaches for something
    only a stdlib pool has, this is what stops compiling.
    """

    def submit(self, fn, /, *args, **kwargs):
        future: Future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # a pool reports through the future, never raises
            future.set_exception(exc)
        return future


def _process_pool(method: str):
    return ProcessPoolExecutor(2, mp_context=multiprocessing.get_context(method))


#: Every executor shape the docs name, and the reason each is here.
#:
#: **`fork` is absent and that is the statement.** polars' thread pool does not
#: survive it, and a forked worker *hangs* rather than failing — so it cannot be
#: a parametrisation without wedging CI, which is exactly why the docs refuse
#: it. Measured: `fork` never returns where all four below do.
EXECUTORS = [
    pytest.param(Inline, id='inline-protocol'),
    pytest.param(lambda: ThreadPoolExecutor(2), id='threads'),
    pytest.param(lambda: _process_pool('spawn'), id='processes-spawn'),
    pytest.param(
        lambda: _process_pool('forkserver'),
        id='processes-forkserver',
        marks=pytest.mark.skipif(
            'forkserver' not in multiprocessing.get_all_start_methods(),
            reason='forkserver is not available on this platform',
        ),
    ),
]


@pytest.mark.parametrize('make_executor', EXECUTORS)
def test_every_executor_gives_the_same_answers_in_the_same_order(make_executor):
    """One fold, four pools, one answer — and the sequential run is the oracle.

    Same numbers *and* the same order. Futures complete out of order, so a
    sweep that read them by completion would reorder itself run to run, which
    is the kind of wrong that looks fine until two runs are diffed.
    """
    sources = scenario_sources()
    sequential = lps.solve_over(DISPATCH, sources, lps.EachCoordinate('scenario'), keep=('p',))

    pool = make_executor()
    if hasattr(pool, '__enter__'):
        with pool as live:
            parallel = lps.solve_over(DISPATCH, sources, lps.EachCoordinate('scenario'), keep=('p',), executor=live)
    else:
        parallel = lps.solve_over(DISPATCH, sources, lps.EachCoordinate('scenario'), keep=('p',), executor=pool)

    assert parallel.keys == sequential.keys
    assert parallel.objective.equals(sequential.objective)
    assert parallel.primal('p').equals(sequential.primal('p'))


def test_a_thread_pool_does_not_encode_for_a_boundary_it_never_crosses():
    """In-process, so a parquet round trip would be paid for nothing.

    31% of a thread-pool sweep, measured, which is what earns the one type
    check in the driver. `ThreadPoolExecutor` is public stdlib, so this is a
    documented class rather than a reach into an executor's internals — and
    every other executor is assumed to cross, because none of them can be
    asked.
    """
    seen: list[str] = []
    original = strategy._encode

    def spy(sources, *, shared_fs):
        seen.append('encoded')
        return original(sources, shared_fs=shared_fs)

    strategy._encode = spy
    try:
        with ThreadPoolExecutor(2) as pool:
            lps.solve_over(DISPATCH, scenario_sources(), lps.EachCoordinate('scenario'), executor=pool)
        assert seen == [], 'a thread pool encoded its sources'

        with ProcessPoolExecutor(2, mp_context=multiprocessing.get_context('spawn')) as pool:
            lps.solve_over(DISPATCH, scenario_sources(), lps.EachCoordinate('scenario'), executor=pool)
        assert seen, 'a process pool did not encode its sources'
    finally:
        strategy._encode = original


def test_a_failing_slice_reports_the_real_error_across_a_process_boundary():
    """An exception has to survive pickling or the cause is lost.

    A custom ``__init__`` signature is the classic way this breaks, and it
    surfaces as an unrelated ``TypeError`` raised while *unpickling* — so the
    worker's real complaint never arrives. Pinned because a future improvement
    to an error message is exactly what would break it.
    """
    broken = {**scenario_sources()}
    broken.pop('cost')
    with (
        ProcessPoolExecutor(2, mp_context=multiprocessing.get_context('spawn')) as pool,
        pytest.raises(lps.DataError, match="no data provided for parameter 'cost'"),
    ):
        lps.solve_over(DISPATCH, broken, lps.EachCoordinate('scenario'), executor=pool)


def test_a_parquet_path_slices_without_being_read_whole(tmp_path):
    """A path source is scanned, so the per-slice filter pushes into the file."""
    sources = scenario_sources()
    path = tmp_path / 'load.parquet'
    frame = sources.pop('load')
    assert isinstance(frame, pl.DataFrame)
    frame.write_parquet(path)

    runs = lps.solve_over(DISPATCH, {**sources, 'load': str(path)}, lps.EachCoordinate('scenario'), keep=('p',))
    assert runs.keys == ['high', 'low', 'mid']
    assert runs.primal('p').height == 3 * 4 * 2


def test_a_hand_built_axis_needs_no_class():
    """`axis` also takes a plain list of `(key, sources, coords)`, so an
    irregular ladder needs no third constructor on the public surface."""
    base = scenario_sources()
    cuts = [
        (name, {**base, 'load': base['load'].filter(pl.col('scenario') == name).drop('scenario')}, {})
        for name in ('low', 'high')
    ]
    runs = lps.solve_over(DISPATCH, base, cuts, keep=('p',))
    assert runs.keys == ['low', 'high']
    assert runs.meta.columns[0] == 'slice'
