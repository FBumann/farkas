"""piecewise costs: the λ-formulation block, and the epigraph that needs none.

The ``piecewise:`` expansion runs before either backend, so eager and
relational receive identical affine declarations. Nonconvex correctness is
verified by checking the linked primals lie ON the curve (adjacency binaries
at work) against a numpy interpolation; the ``convex:`` flag is verified to
produce the hull instead.

The last section is the counterweight, and SPEC §12's claim: convex piecewise
needs no formulation machinery at all. Written as epigraph constraints it is
ordinary affine YAML, relational-eligible with no ``piecewise:`` block in
sight — which is the reason the block is only for the nonconvex case.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from farkas.lowering import lower_program
from farkas.piecewise import (
    PiecewiseExpansionError,
    expand_piecewise,
    validate_piecewise_data,
)
from farkas.sources import tidy_sources
from tests.conftest import override, raw_of, schema_of
from tests.differential import differential
from tests.oracle import farkas_linopy

NONCONVEX_YAML = """
dimensions:
  snapshot: {dtype: int}
  bp: {dtype: int}

parameters:
  load: {dims: [snapshot]}
  bp_x: {dims: [bp]}
  bp_y: {dims: [bp]}

variables:
  p:
    foreach: [snapshot]
    bounds: {lower: 0, upper: 100}
  op_cost:
    foreach: [snapshot]
    bounds: {lower: 0}

piecewise:
  cost_curve:
    over: bp
    links:
      - [p, bp_x]
      - [op_cost, bp_y]

constraints:
  balance:
    foreach: [snapshot]
    equations:
      - expression: p == load

objectives:
  total:
    sense: minimize
    equations:
      - expression: sum(op_cost, over=snapshot)
"""


def curve(p, bp_x, bp_y) -> float:
    return float(np.interp(p, np.asarray(bp_x), np.asarray(bp_y)))


@pytest.fixture
def nonconvex_inputs():
    rng = np.random.default_rng(13)
    n_s = 12
    # concave curve (economies of scale): slopes 0.75 then ~0.417 — the
    # convex hull's lower envelope (the chord) would undercut it, so the
    # adjacency binaries are load-bearing
    bp_x = pd.Series([0.0, 40.0, 100.0], index=pd.RangeIndex(3, name='bp'))
    bp_y = pd.Series([0.0, 30.0, 55.0], index=pd.RangeIndex(3, name='bp'))
    load = pd.Series(rng.uniform(5, 95, n_s).round(2), index=pd.RangeIndex(n_s, name='snapshot'))
    data = {'load': load, 'bp_x': bp_x, 'bp_y': bp_y}
    coords = {'snapshot': load.index, 'bp': bp_x.index}
    return data, coords


# ---------------------------------------------------------------------------
# the λ formulation, end to end
# ---------------------------------------------------------------------------


def test_the_solution_sits_on_the_curve_not_on_its_hull(nonconvex_inputs):
    data, coords = nonconvex_inputs
    expected = sum(curve(v, data['bp_x'], data['bp_y']) for v in data['load'])

    with differential(NONCONVEX_YAML, data, coords) as run:
        assert run.oracle == pytest.approx(expected, rel=1e-6)  # ON the curve, not the hull

        cost = run.sol.primal('op_cost').set_index('snapshot')['value']
        for s, load_v in data['load'].items():
            assert cost[s] == pytest.approx(curve(load_v, data['bp_x'], data['bp_y']), abs=1e-6)


def test_the_convex_flag_gives_the_hull_and_stays_a_pure_lp(nonconvex_inputs, tmp_path):
    # same concave curve with convex: true — the LP relaxation's lower
    # envelope is the chord, so the objective must drop BELOW the curve
    data, coords = nonconvex_inputs
    yaml_text = NONCONVEX_YAML.replace('over: bp', 'over: bp\n    convex: true')

    program = lower_program(schema_of(yaml_text))  # inside the streaming language
    assert all(v.variable_type == 'continuous' for v in program.variables)

    path = tmp_path / 'hull.yaml'
    path.write_text(yaml_text)
    m = farkas_linopy.build(path, data=data, coords=coords)
    m.solve(solver_name='highs', output_flag=False)

    on_curve = sum(curve(v, data['bp_x'], data['bp_y']) for v in data['load'])
    chord = sum(0.55 * v for v in data['load'])  # (100, 55) chord from origin
    assert float(m.objective.value) == pytest.approx(chord, rel=1e-6)
    assert float(m.objective.value) < on_curve


CHP_YAML = """
dimensions:
  snapshot: {dtype: int}
  bp: {dtype: int}

parameters:
  load: {dims: [snapshot]}
  power_bp: {dims: [bp]}
  fuel_bp: {dims: [bp]}
  heat_bp: {dims: [bp]}

variables:
  power:
    foreach: [snapshot]
    bounds: {lower: 0, upper: 100}
  fuel:
    foreach: [snapshot]
    bounds: {lower: 0}
  heat:
    foreach: [snapshot]
    bounds: {lower: 0}

piecewise:
  chp:
    over: bp
    links:
      - [power, power_bp]
      - [fuel, fuel_bp]
      - [heat, heat_bp]

constraints:
  balance:
    foreach: [snapshot]
    equations:
      - expression: power == load

objectives:
  total:
    sense: minimize
    equations:
      - expression: sum(fuel, over=snapshot)
"""


def test_three_links_all_track_the_same_curve_position():
    n_s = 8
    rng = np.random.default_rng(21)
    power_bp = pd.Series([0.0, 50.0, 100.0], index=pd.RangeIndex(3, name='bp'))
    fuel_bp = pd.Series([10.0, 60.0, 140.0], index=pd.RangeIndex(3, name='bp'))
    heat_bp = pd.Series([0.0, 20.0, 60.0], index=pd.RangeIndex(3, name='bp'))
    load = pd.Series(rng.uniform(10, 90, n_s).round(2), index=pd.RangeIndex(n_s, name='snapshot'))
    data = {'load': load, 'power_bp': power_bp, 'fuel_bp': fuel_bp, 'heat_bp': heat_bp}
    coords = {'snapshot': load.index, 'bp': power_bp.index}

    with differential(CHP_YAML, data, coords) as run:
        fuel = run.sol.primal('fuel').set_index('snapshot')['value']
        heat = run.sol.primal('heat').set_index('snapshot')['value']
        for s, load_v in load.items():
            assert fuel[s] == pytest.approx(curve(load_v, power_bp, fuel_bp), abs=1e-6)
            assert heat[s] == pytest.approx(curve(load_v, power_bp, heat_bp), abs=1e-6)


GATED_YAML = """
dimensions:
  snapshot: {dtype: int}
  bp: {dtype: int}

parameters:
  load: {dims: [snapshot]}
  on_flag: {dims: [snapshot]}
  bp_x: {dims: [bp]}
  bp_y: {dims: [bp]}

variables:
  u:
    foreach: [snapshot]
    binary: true
  p:
    foreach: [snapshot]
    bounds: {lower: 0, upper: 100}
  op_cost:
    foreach: [snapshot]
    bounds: {lower: 0}

piecewise:
  cost_curve:
    over: bp
    links:
      - [p, bp_x]
      - [op_cost, bp_y]
    active: u

constraints:
  commit:
    foreach: [snapshot]
    equations:
      - expression: u == on_flag
  balance:
    foreach: [snapshot]
    equations:
      - expression: p == load * on_flag

objectives:
  total:
    sense: minimize
    equations:
      - expression: sum(op_cost, over=snapshot)
"""


def test_active_gates_the_curve_off(nonconvex_inputs):
    data, coords = nonconvex_inputs
    on_flag = pd.Series([1.0, 0.0] * 6, index=pd.RangeIndex(12, name='snapshot'))
    data = {**data, 'on_flag': on_flag}

    with differential(GATED_YAML, data, coords) as run:
        cost = run.sol.primal('op_cost').set_index('snapshot')['value']
        for s in on_flag.index:
            # on: cost sits ON the curve at the pinned load; off: pinned to zero
            expected = curve(data['load'][s], data['bp_x'], data['bp_y']) if on_flag[s] else 0.0
            assert cost[s] == pytest.approx(expected, abs=1e-6)


def test_breakpoints_may_vary_along_another_dim():
    """examples/piecewise.yaml: convex per-generator curves (breakpoints vary
    along the generator dim — the thing flat breakpoint lists can't do)."""
    import xarray as xr

    example = Path('examples/piecewise.yaml')
    rng = np.random.default_rng(31)
    n_s = 24
    gens = pd.Index(['cheap', 'mid'], name='generator')
    bps = pd.RangeIndex(3, name='bp')
    p_max = pd.Series({'cheap': 100.0, 'mid': 120.0})
    # convex per-generator curves: increasing marginal cost, different shapes
    bp_x = xr.DataArray([[0.0, 40.0, 100.0], [0.0, 60.0, 120.0]], coords={'generator': gens, 'bp': bps})
    bp_y = xr.DataArray([[0.0, 200.0, 800.0], [0.0, 900.0, 2700.0]], coords={'generator': gens, 'bp': bps})
    load = pd.Series(
        (rng.uniform(0.3, 0.9, n_s) * p_max.sum()).round(1),
        index=pd.RangeIndex(n_s, name='snapshot'),
    )
    data = {'p_max': p_max, 'load': load, 'bp_x': bp_x, 'bp_y': bp_y}
    coords = {'snapshot': load.index, 'generator': gens, 'bp': bps}

    lower_program(schema_of(example))  # inside the streaming language (convex: pure LP)

    with differential(example, data, coords) as run:
        # each generator's cost sits on its own curve (hull is exact: convex + min)
        p = run.sol.primal('p').set_index(['snapshot', 'generator'])['value']
        cost = run.sol.primal('op_cost').set_index(['snapshot', 'generator'])['value']
        for (s, g), pv in p.items():
            expected = curve(pv, bp_x.sel(generator=g), bp_y.sel(generator=g))
            assert cost[(s, g)] == pytest.approx(expected, abs=1e-5)


# ---------------------------------------------------------------------------
# what the expansion emits, and what it refuses
# ---------------------------------------------------------------------------


def test_expansion_emits_the_lambda_declarations():
    expanded = expand_piecewise(schema_of(NONCONVEX_YAML))

    assert not expanded.piecewise
    assert 'cost_curve_lam' in expanded.variables
    assert expanded.variables['cost_curve_seg'].binary
    assert set(expanded.constraints) >= {
        'cost_curve_convexity',
        'cost_curve_pick',
        'cost_curve_adjacency',
        'cost_curve_link0',
        'cost_curve_link1',
        'balance',
    }


def test_an_inline_expression_is_a_legal_link():
    raw = raw_of(NONCONVEX_YAML)
    raw['piecewise']['cost_curve']['links'][0] = ['p * 2', 'bp_x']
    expanded = expand_piecewise(schema_of(raw))
    assert expanded.constraints['cost_curve_link0'].equations[0].expression.startswith('(p * 2) ==')


def test_a_link_naming_an_undeclared_parameter_is_refused():
    raw = raw_of(NONCONVEX_YAML)
    raw['piecewise']['cost_curve']['links'][1][1] = 'nope'
    with pytest.raises(PiecewiseExpansionError, match="undeclared parameter 'nope'"):
        expand_piecewise(schema_of(raw))


@pytest.mark.parametrize(
    ('model', 'patch', 'match'),
    [
        (
            NONCONVEX_YAML,
            {'piecewise.cost_curve.links': [['p', 'bp_x', '<='], ['op_cost', 'bp_y', '>=']]},
            'at most one link',
        ),
        (CHP_YAML, {'piecewise.chp.convex': True}, 'exactly two links'),
        (GATED_YAML, {'variables.u': {'foreach': ['snapshot'], 'bounds': {'lower': 0, 'upper': 1}}}, 'must be binary'),
    ],
)
def test_a_malformed_block_is_refused(model, patch, match):
    """Schema-level arity rules and the expansion's own preconditions — both
    have to fire before any data is bound."""
    with pytest.raises(ValueError, match=match):
        expand_piecewise(schema_of(model, **patch))


@pytest.mark.parametrize(
    ('link_expression', 'message'),
    [
        ('p ** 2', r"operator '\*\*'"),
        ('p * p', 'both factors of a product contain variables'),
    ],
)
def test_a_link_outside_the_language_is_named_where_the_user_wrote_it(link_expression, message):
    """The formulation checks its links itself, and that is the whole point.

    Lowering would catch these anyway — but only after expansion, so the error
    would name ``cost_curve_link0``, a declaration the user never wrote. The
    guard in ``_expr_dims`` exists to keep the message pointing at the
    ``piecewise:`` block and the link index instead.
    """
    raw = raw_of(NONCONVEX_YAML)
    block = next(iter(raw['piecewise']))
    raw['piecewise'][block]['links'][0][0] = link_expression

    with pytest.raises(PiecewiseExpansionError, match=message) as exc:
        expand_piecewise(schema_of(raw))
    assert f"piecewise '{block}' link 0" in str(exc.value)


def test_both_lanes_check_the_declarations_a_formulation_emits(tmp_path):
    """Emitted declarations are language too, so both lanes must judge them.

    A link's dims come from its values parameter, so a values parameter
    carrying a dim the links do not is a stray dim in generated math — one row
    per zone where the file reads as one per snapshot. The native lane used to
    validate the file as written, which made ``ly.check()`` pass on a model
    ``farkas_linopy.build`` refused: the same YAML, two answers (hard rule 3).
    """
    import yaml as pyyaml

    import farkas as ly
    from farkas.errors import DimensionError

    raw = override(
        raw_of(NONCONVEX_YAML),
        **{'dimensions.zone': {'dtype': 'str'}, 'parameters.bp_y': {'dims': ['zone', 'bp']}},
    )
    stray = r"cost_curve_link1.*\['zone'\]"

    with pytest.raises(DimensionError, match=stray):
        ly.check(raw)

    path = tmp_path / 'stray_dim.yaml'
    path.write_text(pyyaml.safe_dump(raw))
    with pytest.raises(DimensionError, match=stray):
        farkas_linopy.build(path)


# ---------------------------------------------------------------------------
# the data guard: `convex: true` is a promise about the breakpoints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('breakpoints', 'match'),
    [
        # convex then concave — the hull would silently cut corners
        (
            {
                'bp_x': pd.Series([0.0, 30.0, 60.0, 100.0], index=pd.RangeIndex(4, name='bp')),
                'bp_y': pd.Series([0.0, 10.0, 40.0, 50.0], index=pd.RangeIndex(4, name='bp')),
            },
            'mixed-curvature',
        ),
        ({'bp_x': pd.Series([0.0, 50.0, 40.0], index=pd.RangeIndex(3, name='bp'))}, 'strictly increasing'),
    ],
)
def test_convex_breakpoints_that_are_not_convex_are_refused(nonconvex_inputs, breakpoints, match):
    data, _ = nonconvex_inputs
    schema = schema_of(NONCONVEX_YAML, **{'piecewise.cost_curve.convex': True})

    with pytest.raises(PiecewiseExpansionError, match=match):
        validate_piecewise_data(schema, {**data, **breakpoints})


def test_the_curvature_guard_also_fires_through_the_relational_adapter(nonconvex_inputs):
    """`tidy_sources` is the streaming lane's only door for data, so the guard
    has to live behind it too — not only in the eager loader."""
    data, coords = nonconvex_inputs
    schema = schema_of(NONCONVEX_YAML, **{'piecewise.cost_curve.convex': True})

    validate_piecewise_data(schema, data)  # consistent (concave) curvature passes

    bad = {**data, 'bp_x': pd.Series([0.0, 50.0, 40.0], index=pd.RangeIndex(3, name='bp'))}
    with pytest.raises(PiecewiseExpansionError, match='strictly increasing'):
        tidy_sources(schema, bad, coords)


# ---------------------------------------------------------------------------
# the counterweight: convex piecewise with no formulation at all
# ---------------------------------------------------------------------------

EPIGRAPH_YAML = """
# The epigraph pattern: convex piecewise costs in ordinary affine YAML,
# no piecewise: block needed (the seed of issue #23's method: lp).
dimensions:
  snapshot: {dtype: int}
  generator: {dtype: str}
  segment: {dtype: str}

parameters:
  p_max: {dims: [generator]}
  load: {dims: [snapshot]}
  seg_slope: {dims: [generator, segment]}
  seg_intercept: {dims: [generator, segment]}

variables:
  p:
    foreach: [snapshot, generator]
    bounds: {lower: 0, upper: p_max}
  gen_cost:
    foreach: [snapshot, generator]
    bounds: {lower: 0}

constraints:
  balance:
    foreach: [snapshot]
    equations:
      - expression: sum(p, over=generator) == load
  pwl:
    foreach: [snapshot, generator, segment]
    equations:
      - expression: gen_cost >= p * seg_slope + seg_intercept

objectives:
  total_cost:
    sense: minimize
    equations:
      - expression: sum(gen_cost, over=generator)
"""


@pytest.fixture
def epigraph_inputs():
    import xarray as xr

    rng = np.random.default_rng(9)
    n_s = 24
    gens = ['cheap', 'mid']
    segments = ['s0', 's1', 's2']
    p_max = pd.Series({'cheap': 100.0, 'mid': 120.0})

    # convex piecewise cost: increasing marginal cost per segment.
    # tangent k of a convex curve: cost >= slope_k * p + intercept_k
    slopes = pd.DataFrame({'cheap': [5.0, 15.0, 40.0], 'mid': [20.0, 35.0, 60.0]}, index=segments)
    # breakpoints at 40% and 75% of p_max; intercepts make tangents touch
    intercepts = {}
    for g in gens:
        b1, b2 = 0.4 * p_max[g], 0.75 * p_max[g]
        s0, s1, s2 = slopes[g]
        intercepts[g] = [0.0, (s0 - s1) * b1, (s0 - s1) * b1 + (s1 - s2) * b2]
    icepts = pd.DataFrame(intercepts, index=segments)

    load = pd.Series(
        (rng.uniform(0.3, 0.9, n_s) * p_max.sum()).round(1),
        index=pd.RangeIndex(n_s, name='snapshot'),
    )
    data = {
        'p_max': p_max,
        'load': load,
        'seg_slope': xr.DataArray.from_series(slopes.T.stack().rename_axis(['generator', 'segment'])),
        'seg_intercept': xr.DataArray.from_series(icepts.T.stack().rename_axis(['generator', 'segment'])),
    }
    coords = {
        'snapshot': load.index,
        'generator': pd.Index(gens, name='generator'),
        'segment': pd.Index(segments, name='segment'),
    }
    return data, coords


def test_the_epigraph_pattern_needs_no_formulation_machinery(epigraph_inputs):
    data, coords = epigraph_inputs

    program = lower_program(schema_of(EPIGRAPH_YAML))
    assert all(v.variable_type == 'continuous' for v in program.variables)  # pure LP

    with differential(EPIGRAPH_YAML, data, coords, lp=True) as run:
        # gen_cost equals the true piecewise cost at the optimal dispatch
        # (epigraph is tight under minimisation)
        p = run.sol.primal('p').set_index(['snapshot', 'generator'])['value']
        gc = run.sol.primal('gen_cost').set_index(['snapshot', 'generator'])['value']
        slopes = data['seg_slope'].to_series().unstack('segment')
        icepts = data['seg_intercept'].to_series().unstack('segment')
        for (s, g), pv in p.items():
            expected = max(sl * pv + ic for sl, ic in zip(slopes.loc[g], icepts.loc[g], strict=True))
            assert gc[(s, g)] == pytest.approx(expected, abs=1e-6)
