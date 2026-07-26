"""piecewise: blocks — λ expansion, both backends, linopy-shaped links.

The expansion runs before either backend, so eager and relational receive
identical affine declarations. Nonconvex correctness is verified by checking
the linked primals lie ON the curve (adjacency binaries at work) against a
numpy interpolation; the convex flag is verified to produce the hull instead.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml as pyyaml

from linopy_yaml.lowering import lower_program
from linopy_yaml.piecewise import (
    PiecewiseExpansionError,
    expand_piecewise,
)
from linopy_yaml.relational import DuckdbExecutor
from linopy_yaml.schema import MathSchema
from linopy_yaml.sources import tidy_sources
from tests.oracle import compat

RTOL = 1e-9

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


def curve(p, bp_x, bp_y):
    return float(np.interp(p, bp_x.to_numpy(), bp_y.to_numpy()))


def test_nonconvex_on_curve_differential(nonconvex_inputs, tmp_path):
    data, coords = nonconvex_inputs
    yaml_path = tmp_path / 'pw.yaml'
    yaml_path.write_text(NONCONVEX_YAML)

    m = compat.build(yaml_path, data=data, coords=coords)
    m.solve(solver_name='highs', output_flag=False)
    oracle = float(m.objective.value)
    expected = sum(curve(v, data['bp_x'], data['bp_y']) for v in data['load'])
    assert oracle == pytest.approx(expected, rel=1e-6)  # ON the curve, not the hull

    schema = MathSchema(**pyyaml.safe_load(NONCONVEX_YAML))
    with DuckdbExecutor(memory_limit='256MB') as ex:
        ex.build(lower_program(schema), tidy_sources(schema, data, coords))
        sol = ex.solve()
        assert sol.status == 'Optimal'
        assert sol.objective == pytest.approx(oracle, rel=RTOL)

        cost = sol.primal('op_cost').set_index('snapshot')['value']
        for s, load_v in data['load'].items():
            assert cost[s] == pytest.approx(curve(load_v, data['bp_x'], data['bp_y']), abs=1e-6)


def test_convex_flag_gives_hull(nonconvex_inputs, tmp_path):
    # same concave curve with convex: true — the LP relaxation's lower
    # envelope is the chord, so the objective must drop BELOW the curve
    data, coords = nonconvex_inputs
    yaml_text = NONCONVEX_YAML.replace('over: bp', 'over: bp\n    convex: true')
    yaml_path = tmp_path / 'pw_hull.yaml'
    yaml_path.write_text(yaml_text)

    schema = MathSchema(**pyyaml.safe_load(yaml_text))
    program = lower_program(schema)  # inside the streaming language (pure LP)
    assert all(v.variable_type == 'continuous' for v in program.variables)

    m = compat.build(yaml_path, data=data, coords=coords)
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


def test_chp_three_links(tmp_path):
    n_s = 8
    rng = np.random.default_rng(21)
    power_bp = pd.Series([0.0, 50.0, 100.0], index=pd.RangeIndex(3, name='bp'))
    fuel_bp = pd.Series([10.0, 60.0, 140.0], index=pd.RangeIndex(3, name='bp'))
    heat_bp = pd.Series([0.0, 20.0, 60.0], index=pd.RangeIndex(3, name='bp'))
    load = pd.Series(rng.uniform(10, 90, n_s).round(2), index=pd.RangeIndex(n_s, name='snapshot'))
    data = {'load': load, 'power_bp': power_bp, 'fuel_bp': fuel_bp, 'heat_bp': heat_bp}
    coords = {'snapshot': load.index, 'bp': power_bp.index}

    yaml_path = tmp_path / 'chp.yaml'
    yaml_path.write_text(CHP_YAML)
    m = compat.build(yaml_path, data=data, coords=coords)
    m.solve(solver_name='highs', output_flag=False)
    oracle = float(m.objective.value)
    assert np.isfinite(oracle)

    schema = MathSchema(**pyyaml.safe_load(CHP_YAML))
    with DuckdbExecutor(memory_limit='256MB') as ex:
        ex.build(lower_program(schema), tidy_sources(schema, data, coords))
        sol = ex.solve()
        assert sol.status == 'Optimal'
        assert sol.objective == pytest.approx(oracle, rel=RTOL)

        # all three linked primals sit at the same curve position
        fuel = sol.primal('fuel').set_index('snapshot')['value']
        heat = sol.primal('heat').set_index('snapshot')['value']
        for s, load_v in load.items():
            assert fuel[s] == pytest.approx(curve(load_v, power_bp, fuel_bp), abs=1e-6)
            assert heat[s] == pytest.approx(curve(load_v, power_bp, heat_bp), abs=1e-6)


def test_expansion_structure_and_errors():
    schema = MathSchema(**pyyaml.safe_load(NONCONVEX_YAML))
    expanded = expand_piecewise(schema)
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
    # inline expressions are legal links
    raw = pyyaml.safe_load(NONCONVEX_YAML)
    raw['piecewise']['cost_curve']['links'][0] = ['p * 2', 'bp_x']
    expanded = expand_piecewise(MathSchema(**raw))
    eq = expanded.constraints['cost_curve_link0'].equations[0].expression
    assert eq.startswith('(p * 2) ==')

    raw = pyyaml.safe_load(NONCONVEX_YAML)
    raw['piecewise']['cost_curve']['links'][1][1] = 'nope'
    with pytest.raises(PiecewiseExpansionError, match="undeclared parameter 'nope'"):
        expand_piecewise(MathSchema(**raw))

    raw = pyyaml.safe_load(NONCONVEX_YAML)
    raw['piecewise']['cost_curve']['links'] = [
        ['p', 'bp_x', '<='],
        ['op_cost', 'bp_y', '>='],
    ]
    with pytest.raises(ValueError, match='at most one link'):
        MathSchema(**raw)


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


def test_active_gating(nonconvex_inputs, tmp_path):
    data, coords = nonconvex_inputs
    on_flag = pd.Series([1.0, 0.0] * 6, index=pd.RangeIndex(12, name='snapshot'))
    data = {**data, 'on_flag': on_flag}

    yaml_path = tmp_path / 'gated.yaml'
    yaml_path.write_text(GATED_YAML)
    m = compat.build(yaml_path, data=data, coords=coords)
    m.solve(solver_name='highs', output_flag=False)
    oracle = float(m.objective.value)
    assert np.isfinite(oracle)

    schema = MathSchema(**pyyaml.safe_load(GATED_YAML))
    with DuckdbExecutor(memory_limit='256MB') as ex:
        ex.build(lower_program(schema), tidy_sources(schema, data, coords))
        sol = ex.solve()
        assert sol.status == 'Optimal'
        assert sol.objective == pytest.approx(oracle, rel=RTOL)

        cost = sol.primal('op_cost').set_index('snapshot')['value']
        for s in on_flag.index:
            # on: cost sits ON the curve at the pinned load; off: pinned to zero
            expected = curve(data['load'][s], data['bp_x'], data['bp_y']) if on_flag[s] else 0.0
            assert cost[s] == pytest.approx(expected, abs=1e-6)


def test_both_lanes_check_the_declarations_a_formulation_emits(tmp_path):
    """Emitted declarations are language too, so both lanes must judge them.

    A link's dims come from its values parameter, so a values parameter
    carrying a dim the links do not is a stray dim in generated math — one row
    per zone where the file reads as one per snapshot. The native lane used to
    validate the file as written, which made ``ly.check()`` pass on a model
    ``compat.build`` refused: the same YAML, two answers (hard rule 3).
    """
    import linopy_yaml as ly
    from linopy_yaml.errors import DimensionError

    raw = pyyaml.safe_load(NONCONVEX_YAML)
    raw['dimensions']['zone'] = {'dtype': 'str'}
    raw['parameters']['bp_y'] = {'dims': ['zone', 'bp']}
    stray = r"cost_curve_link1.*\['zone'\]"

    with pytest.raises(DimensionError, match=stray):
        ly.check(raw)

    path = tmp_path / 'stray_dim.yaml'
    path.write_text(pyyaml.safe_dump(raw))
    with pytest.raises(DimensionError, match=stray):
        compat.build(path)


def test_active_must_be_binary():
    raw = pyyaml.safe_load(GATED_YAML)
    raw['variables']['u'] = {
        'foreach': ['snapshot'],
        'bounds': {'lower': 0, 'upper': 1},
    }
    with pytest.raises(PiecewiseExpansionError, match='must be binary'):
        expand_piecewise(MathSchema(**raw))


def test_convex_guard_rejects_mixed_curvature(nonconvex_inputs):
    from linopy_yaml.piecewise import validate_piecewise_data

    data, coords = nonconvex_inputs
    raw = pyyaml.safe_load(NONCONVEX_YAML)
    raw['piecewise']['cost_curve']['convex'] = True
    schema = MathSchema(**raw)

    # mixed curvature: convex then concave — hull would silently cut corners
    bad = {
        **data,
        'bp_x': pd.Series([0.0, 30.0, 60.0, 100.0], index=pd.RangeIndex(4, name='bp')),
        'bp_y': pd.Series([0.0, 10.0, 40.0, 50.0], index=pd.RangeIndex(4, name='bp')),
    }
    with pytest.raises(PiecewiseExpansionError, match='mixed-curvature'):
        validate_piecewise_data(schema, bad)

    # non-monotone x breakpoints
    bad = {
        **data,
        'bp_x': pd.Series([0.0, 50.0, 40.0], index=pd.RangeIndex(3, name='bp')),
    }
    with pytest.raises(PiecewiseExpansionError, match='strictly increasing'):
        validate_piecewise_data(schema, bad)

    # consistent (concave) curvature passes — hull semantics documented
    validate_piecewise_data(schema, data)

    # the guard also fires via the relational adapter
    with pytest.raises(PiecewiseExpansionError, match='strictly increasing'):
        tidy_sources(schema, bad, coords)


def test_convex_requires_two_links():
    raw = pyyaml.safe_load(CHP_YAML)
    raw['piecewise']['chp']['convex'] = True
    with pytest.raises(ValueError, match='exactly two links'):
        MathSchema(**raw)


def test_example_per_generator_curves(tmp_path):
    """examples/piecewise.yaml: convex per-generator curves (breakpoints vary
    along the generator dim — the thing flat breakpoint lists can't do)."""
    import xarray as xr

    example = 'examples/piecewise.yaml'
    rng = np.random.default_rng(31)
    n_s = 24
    gens = pd.Index(['cheap', 'mid'], name='generator')
    bps = pd.RangeIndex(3, name='bp')
    p_max = pd.Series({'cheap': 100.0, 'mid': 120.0})
    # convex per-generator curves: increasing marginal cost, different shapes
    bp_x = xr.DataArray(
        [[0.0, 40.0, 100.0], [0.0, 60.0, 120.0]],
        coords={'generator': gens, 'bp': bps},
    )
    bp_y = xr.DataArray(
        [[0.0, 200.0, 800.0], [0.0, 900.0, 2700.0]],
        coords={'generator': gens, 'bp': bps},
    )
    load = pd.Series(
        (rng.uniform(0.3, 0.9, n_s) * p_max.sum()).round(1),
        index=pd.RangeIndex(n_s, name='snapshot'),
    )
    data = {'p_max': p_max, 'load': load, 'bp_x': bp_x, 'bp_y': bp_y}
    coords = {'snapshot': load.index, 'generator': gens, 'bp': bps}

    schema = MathSchema(**pyyaml.safe_load(Path(example).read_text()))
    lower_program(schema)  # inside the streaming language (convex: pure LP)

    m = compat.build(example, data=data, coords=coords)
    m.solve(solver_name='highs', output_flag=False)
    oracle = float(m.objective.value)
    assert np.isfinite(oracle)

    with DuckdbExecutor(memory_limit='256MB') as ex:
        ex.build(lower_program(schema), tidy_sources(schema, data, coords))
        sol = ex.solve()
        assert sol.status == 'Optimal'
        assert sol.objective == pytest.approx(oracle, rel=RTOL)

        # each generator's cost sits on its own curve (hull is exact: convex + min)
        p = sol.primal('p').set_index(['snapshot', 'generator'])['value']
        cost = sol.primal('op_cost').set_index(['snapshot', 'generator'])['value']
        for (s, g), pv in p.items():
            expected = curve(pv, bp_x.sel(generator=g).to_series(), bp_y.sel(generator=g).to_series())
            assert cost[(s, g)] == pytest.approx(expected, abs=1e-5)


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
    schema = pyyaml.safe_load(NONCONVEX_YAML)
    block = next(iter(schema['piecewise']))
    schema['piecewise'][block]['links'][0][0] = link_expression

    with pytest.raises(PiecewiseExpansionError, match=message) as exc:
        expand_piecewise(MathSchema(**schema))
    assert f"piecewise '{block}' link 0" in str(exc.value)
