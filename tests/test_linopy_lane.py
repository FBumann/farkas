"""The opt-in linopy lane: the shim, its loader, its where evaluator, its notes.

Everything here needs the ``[linopy]`` extra and nothing here is reachable
from the native lane, so it is one module rather than four: the guard, the
``linopy.Model`` fixtures and the "write a YAML file, feed it to the shim"
idiom were being restated in each of them.

The shim itself is a *pure producer* — no accessor, no session, no state on
the model. A file's meaning must not depend on what was loaded before it
(ARCHITECTURE.md, hard rule 5), which is what the last section pins.
"""

from __future__ import annotations

import textwrap

import pandas as pd
import pytest

from farkas.errors import LanguageError
from farkas.schema import MathSchema
from tests.oracle import builder, farkas_linopy, linopy, loader, xr


@pytest.fixture
def yaml_file(tmp_path):
    """Write YAML text to a file — the only shape the eager lane accepts."""

    def write(text: str, name: str = 'm.yaml'):
        path = tmp_path / name
        path.write_text(textwrap.dedent(text).lstrip())
        return path

    return write


@pytest.fixture
def model_with():
    """A ``linopy.Model`` carrying named variables over the given coords."""

    def build(**variables):
        m = linopy.Model()
        for name, labels in variables.items():
            dim, values = labels
            m.add_variables(name=name, coords=[pd.Index(values, name=dim)])
        return m

    return build


# ---------------------------------------------------------------------------
# the shim is a pure producer
# ---------------------------------------------------------------------------


def test_nothing_is_patched_onto_linopy_model():
    """Importing farkas_linopy must not touch linopy.Model."""
    assert not hasattr(linopy.Model, 'from_yaml')
    assert not hasattr(linopy.Model, 'yaml')


def test_extend_is_stateless(yaml_file):
    """A second YAML cannot lean on the first: every file declares what it uses.

    This is hard rule 5 — no Python-side state may change what a file means.
    """
    m = linopy.Model()
    first = yaml_file(
        """
        dimensions:
          generator: {values: [wind, solar]}
        parameters:
          cap: {dims: [generator]}
        variables:
          q: {foreach: [generator]}
        constraints:
          limit:
            foreach: [generator]
            equations:
              - expression: q <= cap
        """,
        'first.yaml',
    )
    farkas_linopy.extend(m, first, data={'cap': pd.Series({'wind': 1.0, 'solar': 2.0})})

    # 'q' is a model variable, so the second file may reference it. 'cap' is
    # not redeclared here — and must therefore be unknown.
    second = yaml_file(
        """
        dimensions:
          generator: {}
        constraints:
          limit2:
            foreach: [generator]
            equations:
              - expression: q <= cap
        """,
        'second.yaml',
    )
    with pytest.raises(ValueError, match="'cap' not found"):
        farkas_linopy.extend(m, second)


# ---------------------------------------------------------------------------
# extend(): reconciling a file's dims with the model's
# ---------------------------------------------------------------------------


def test_infer_coords_unions_across_variables(model_with):
    """_infer_coords unions per-dim coordinates across all model variables."""
    m = model_with(a=('generator', ['wind', 'solar']), b=('generator', ['wind', 'gas']))

    inferred = farkas_linopy._infer_coords(m)
    assert set(inferred['generator']) == {'wind', 'solar', 'gas'}


@pytest.mark.parametrize(
    ('declared', 'existing', 'accepted'),
    [
        pytest.param('[wind, solar]', ['wind', 'solar'], True, id='values-match'),
        pytest.param('[a, b]', ['wind', 'solar'], False, id='values-differ'),
        # inference unions across variables, so `values:` must match what was
        # inferred — not merely what some other declaration said
        pytest.param('[wind, gas]', ['wind', 'solar'], False, id='values-differ-from-inferred'),
    ],
)
def test_redeclared_dim_values_must_match_the_existing_model(yaml_file, model_with, declared, existing, accepted):
    m = model_with(p=('generator', existing))
    ext = yaml_file(f'dimensions:\n  generator: {{values: {declared}}}\n')

    if accepted:
        farkas_linopy.extend(m, ext)  # must not raise
    else:
        with pytest.raises(ValueError, match='differ from the existing model'):
            farkas_linopy.extend(m, ext)


def test_extend_falls_back_to_inferred_coords(yaml_file, model_with):
    """Extension YAML may omit values: for dims already on the model."""
    m = model_with(p=('generator', ['wind', 'solar']))
    ext = yaml_file(
        """
        dimensions:
          generator: {}
        parameters:
          cap: {dims: [generator]}
        constraints:
          limit:
            foreach: [generator]
            equations:
              - expression: p <= cap
        """
    )

    farkas_linopy.extend(m, ext, data={'cap': pd.Series({'wind': 1.0, 'solar': 2.0})})
    assert 'limit' in m.constraints


def test_the_coords_kwarg_wins_over_inference(yaml_file, model_with):
    m = model_with(p=('generator', ['wind', 'solar']))
    ext = yaml_file('dimensions:\n  generator: {}\nparameters:\n  cap: {dims: [generator]}\n')

    # must not raise: the override, not inference, defines the dim here
    farkas_linopy.extend(
        m,
        ext,
        data={'cap': pd.Series({'wind': 1.0, 'gas': 3.0})},
        coords={'generator': ['wind', 'gas']},
    )


def test_extend_sees_existing_model_variables(yaml_file, model_with):
    """An extension may reference variables already on the model, and only
    those — an unknown name is the same load error as anywhere else."""
    text = """
        dimensions:
          g: {values: [wind, solar]}
        constraints:
          cap:
            foreach: [g]
            equations:
              - expression: p <= 100
        """
    farkas_linopy.extend(model_with(p=('g', ['wind', 'solar'])), yaml_file(text))

    with pytest.raises(ValueError, match="'p' not found"):
        farkas_linopy.extend(linopy.Model(), yaml_file(text))


# ---------------------------------------------------------------------------
# loader: master coords and parameter coercion
# ---------------------------------------------------------------------------


def _schema(dims=None, params=None) -> MathSchema:
    raw = {}
    if dims:
        raw['dimensions'] = dims
    if params:
        raw['parameters'] = params
    return MathSchema.model_validate(raw)


class TestBuildMasterCoords:
    def test_from_yaml_values(self):
        mc = loader.build_master_coords(_schema(dims={'x': {'values': [1, 2, 3]}}), None)
        assert list(mc['x']) == [1, 2, 3]

    def test_from_coords_kwarg(self):
        mc = loader.build_master_coords(_schema(dims={'x': {}}), {'x': [10, 20]})
        assert list(mc['x']) == [10, 20]

    def test_coords_overrides_yaml(self):
        mc = loader.build_master_coords(_schema(dims={'x': {'values': [1, 2]}}), {'x': [99]})
        assert list(mc['x']) == [99]

    def test_missing_raises(self):
        with pytest.raises(ValueError, match="Dimension 'x' has no values"):
            loader.build_master_coords(_schema(dims={'x': {}}), None)


class TestLoadParameters:
    """Every shape a user may hand a parameter, coerced onto the master coords."""

    @pytest.mark.parametrize(
        ('values', 'data', 'select', 'expected'),
        [
            pytest.param([1, 2], 5.0, {'x': 1}, 5.0, id='scalar-broadcasts'),
            pytest.param(['a', 'b'], {'a': 100, 'b': 60}, {'x': 'a'}, 100.0, id='dict'),
            pytest.param(
                ['a', 'b'],
                pd.Series([1.0, 2.0], index=pd.Index(['a', 'b'], name='x')),
                {'x': 'b'},
                2.0,
                id='series',
            ),
            pytest.param(
                [0, 1],
                xr.DataArray([10, 20], dims=['x'], coords={'x': [0, 1]}),
                {'x': 1},
                20.0,
                id='dataarray',
            ),
        ],
    )
    def test_accepted_shapes(self, values, data, select, expected):
        s = _schema(dims={'x': {'values': values}}, params={'a': {'dims': ['x']}})
        ds = loader.load_parameters(s, {'a': data}, loader.build_master_coords(s, None))
        assert float(ds['a'].sel(**select)) == expected

    def test_missing_required_raises(self):
        s = _schema(dims={'x': {'values': [1]}}, params={'a': {'dims': ['x']}})
        with pytest.raises(ValueError, match='required'):
            loader.load_parameters(s, {}, loader.build_master_coords(s, None))

    def test_unknown_keys_raises(self):
        s = _schema(dims={'x': {'values': [1]}})
        with pytest.raises(ValueError, match='not declared'):
            loader.load_parameters(s, {'extra': 1}, loader.build_master_coords(s, None))

    def test_unexpected_dims_raises(self):
        s = _schema(dims={'x': {'values': [1]}, 'y': {'values': [2]}}, params={'a': {'dims': ['x']}})
        da = xr.DataArray([[1]], dims=['x', 'y'], coords={'x': [1], 'y': [2]})
        with pytest.raises(ValueError, match='unexpected dimensions'):
            loader.load_parameters(s, {'a': da}, loader.build_master_coords(s, None))

    def test_unknown_coord_raises(self):
        s = _schema(dims={'g': {'values': ['a', 'b']}}, params={'p': {'dims': ['g']}})
        series = pd.Series([1.0], index=pd.Index(['z'], name='g'))
        with pytest.raises(ValueError, match='not in the master coordinate'):
            loader.load_parameters(s, {'p': series}, loader.build_master_coords(s, None))


# ---------------------------------------------------------------------------
# builder.evaluate_where: the eager reading of a resolved where AST
# ---------------------------------------------------------------------------


@pytest.fixture
def gens():
    """A dataset and its master coords, with one generator masked out by p_max."""
    ds = xr.Dataset({'p_max': xr.DataArray([100, 0, 50], dims=['g'], coords={'g': ['wind', 'solar', 'gas']})})
    return ds, {'g': pd.Index(['wind', 'solar', 'gas'], name='g')}


def _resolved(text, parameters=('p_max',), dimensions=('g',)):
    """Resolve then evaluate — the evaluator no longer takes strings."""
    from farkas.resolution import Namespace, where_of

    return where_of(text, Namespace((), parameters, dimensions), 'test')


def test_no_where_is_a_scalar_true(gens):
    mask = builder.evaluate_where(None, *gens)
    assert mask.ndim == 0
    assert bool(mask) is True


def test_a_bare_parameter_name_is_an_existence_check(gens):
    assert builder.evaluate_where(_resolved('p_max'), *gens).all()


def test_a_comparison_masks_per_coordinate(gens):
    mask = builder.evaluate_where(_resolved('p_max > 0'), *gens)
    assert [bool(mask.sel(g=g)) for g in ('wind', 'solar', 'gas')] == [True, False, True]


def test_a_dimension_comparison_masks_on_the_coordinate_itself():
    node = _resolved('t > 0', parameters=(), dimensions=('t',))
    mask = builder.evaluate_where(node, xr.Dataset(), {'t': pd.Index([0, 1, 2], name='t')})
    assert [bool(mask.sel(t=t)) for t in (0, 1, 2)] == [False, True, True]


def test_a_missing_parameter_is_a_load_error():
    """Was: a scalar-False mask, i.e. a silently empty model. Resolution
    makes an undeclared name a load error in both lanes."""
    with pytest.raises(LanguageError, match="'nonexistent' not found"):
        _resolved('nonexistent')


# ---------------------------------------------------------------------------
# error notes: the context add_note() carries out of build/extend
# ---------------------------------------------------------------------------

_MINIMAL = """
    dimensions:
      g: {values: [a]}
    variables:
      p:
        foreach: [g]
"""


def _has_note(exc: BaseException, substring: str) -> bool:
    return any(substring in n for n in getattr(exc, '__notes__', []))


@pytest.mark.parametrize(
    ('tail', 'error', 'match', 'context'),
    [
        pytest.param(
            "    where: '<<<'\n",
            ValueError,
            'Failed to parse where string',
            "Variable 'p'",
            id='malformed-where',
        ),
        pytest.param(
            "constraints:\n  c:\n    foreach: [g]\n    equations:\n      - expression: 'p + 1'\n",
            ValueError,
            'exactly one comparison',
            "Constraint 'c'",
            id='constraint-without-comparison',
        ),
        pytest.param(
            "objectives:\n  obj:\n    equations:\n      - expression: 'p == 1'\n",
            ValueError,
            'must not contain a comparison',
            "Objective 'obj'",
            id='objective-with-comparison',
        ),
        pytest.param(
            # valid syntax and dims, but no variable on the LHS: past what
            # validation can see, so the note has to come from the build phase
            "constraints:\n  c:\n    foreach: []\n    equations:\n      - expression: '1 <= 2'\n",
            TypeError,
            None,
            "while building constraint 'c'",
            id='build-phase-failure',
        ),
    ],
)
def test_a_failure_names_the_declaration_and_the_file(yaml_file, tail, error, match, context):
    bad = yaml_file(textwrap.dedent(_MINIMAL).lstrip() + tail, 'bad.yaml')

    with pytest.raises(error, match=match) as ei:
        farkas_linopy.build(bad)

    assert context in str(ei.value) or _has_note(ei.value, context)
    assert _has_note(ei.value, f"while loading YAML '{bad}'")


def test_a_failure_inside_extend_names_the_extension_file(yaml_file, model_with):
    m = model_with(p=('time', [0, 1, 2, 3]))
    ext = yaml_file('dimensions:\n  time: {values: [a, b]}\n', 'ext.yaml')

    with pytest.raises(ValueError) as ei:
        farkas_linopy.extend(m, ext)

    assert _has_note(ei.value, f"while extending with YAML '{ext}'")
