"""The compiler is lazy, and this file is the proof.

No solver, no data, not one row read — a plan node goes in and a query comes
out. That is the seam the split bought: checking what an operator does costs a
compile, not a build and a solve.

These assertions are deliberately about *shape*, not exact text. They pin the
properties docs/ARCHITECTURE.md's admissibility test reads off the plan — which dim
columns survive, whether an aggregate appears, whether a mask becomes a join or
a filter — and leave the query planner free to change underneath them.

The frames the compiler reads are declared here as **empty frames with the
right schemas**. A lazy frame is a plan, so a schema is all it takes to compile
one; supplying rows would mean the test was checking results rather than shape.
"""

from __future__ import annotations

import polars as pl
import pytest

from farkas.errors import LanguageError
from farkas.relational import plan
from farkas.relational.compiler import PolarsCompiler

PROGRAM = plan.Program(
    parameters=(
        plan.ParameterDeclaration('cost', ('generator',)),
        plan.ParameterDeclaration('load', ('snapshot',)),
        plan.ParameterDeclaration('available', ('generator',)),
    ),
    variables=(plan.VariableDeclaration('p', ('snapshot', 'generator')),),
    constraints=(),
    objective=plan.ObjectiveDeclaration('min', plan.Variable('p')),
    dimensions=(
        plan.DimensionDeclaration('snapshot'),
        plan.DimensionDeclaration('generator', coordinates=(('bus', 'bus'),)),
        plan.DimensionDeclaration('bus'),
    ),
)

CARDINALITY = {'snapshot': 24, 'generator': 3, 'bus': 2}

DIMENSIONS = {
    'snapshot': pl.LazyFrame(schema={'val': pl.Int64, 'ord': pl.Int64}),
    'generator': pl.LazyFrame(schema={'val': pl.String, 'ord': pl.Int64, 'bus': pl.String}),
    'bus': pl.LazyFrame(schema={'val': pl.String, 'ord': pl.Int64}),
}
PARAMETERS = {
    'cost': pl.LazyFrame(schema={'generator': pl.String, 'value': pl.Float64}),
    'load': pl.LazyFrame(schema={'snapshot': pl.Int64, 'value': pl.Float64}),
    'available': pl.LazyFrame(schema={'generator': pl.String, 'value': pl.Float64}),
}
VARIABLES = {'p': pl.LazyFrame(schema={'snapshot': pl.Int64, 'generator': pl.String, 'var_label': pl.Int64})}


def compiler(boolean_parameters: frozenset[str] = frozenset()) -> PolarsCompiler:
    return PolarsCompiler(PROGRAM, CARDINALITY, boolean_parameters, PARAMETERS, DIMENSIONS, VARIABLES)


def columns(frame: pl.LazyFrame) -> list[str]:
    return frame.collect_schema().names()


def query(frame: pl.LazyFrame) -> str:
    """The query plan as text — what an admissibility judgement is read off."""
    return frame.explain(optimized=False)


def joins(frame: pl.LazyFrame) -> int:
    """How many joins the plan performs.

    Counted on the header polars prints for each one (``INNER JOIN:``,
    ``LEFT JOIN:``); the matching ``END … JOIN`` carries no colon, so this
    counts joins rather than lines mentioning one.
    """
    return query(frame).count('JOIN:')


# ---------------------------------------------------------------------------
# expressions
# ---------------------------------------------------------------------------


def test_a_variable_compiles_to_one_term_fragment_over_its_dims():
    compiled = compiler().expression(plan.Variable('p'), 'test')
    assert len(compiled.terms) == 1
    assert not compiled.consts
    fragment = compiled.terms[0]
    assert fragment.dims == ('snapshot', 'generator')
    assert fragment.is_term
    assert columns(fragment.frame) == ['snapshot', 'generator', 'var_label', 'coeff']


def test_a_parameter_is_a_constant_fragment_not_a_term():
    compiled = compiler().expression(plan.Parameter('cost'), 'test')
    assert not compiled.terms
    assert compiled.consts[0].dims == ('generator',)
    assert not compiled.consts[0].is_term
    assert columns(compiled.consts[0].frame) == ['generator', 'cval']


def test_addition_concatenates_fragments_rather_than_joining():
    """An LP row is a sum of terms, so ``+`` needs no query at all."""
    compiled = compiler().expression(plan.Variable('p') + plan.Variable('p'), 'test')
    assert len(compiled.terms) == 2


def test_multiplying_a_variable_by_a_parameter_joins_on_the_shared_dim():
    compiled = compiler().expression(plan.Multiply(plan.Variable('p'), plan.Parameter('cost')), 'test')
    fragment = compiled.terms[0]
    assert fragment.dims == ('snapshot', 'generator')
    assert columns(fragment.frame) == ['snapshot', 'generator', 'var_label', 'coeff']
    assert 'JOIN' in query(fragment.frame)


def test_a_product_of_two_variable_carrying_factors_is_refused():
    with pytest.raises(LanguageError, match='nonlinear product'):
        compiler().expression(plan.Multiply(plan.Variable('p'), plan.Variable('p')), 'test')


def test_a_divisor_carrying_variables_is_refused():
    with pytest.raises(LanguageError, match='nonlinear quotient'):
        compiler().expression(plan.Divide(plan.Variable('p'), plan.Variable('p')), 'test')


# ---------------------------------------------------------------------------
# shape operators — each rewrites exactly one dim column
# ---------------------------------------------------------------------------


def test_sum_drops_the_dim_it_sums_over_without_aggregating():
    """The aggregate lives in the terminal assembly, not in the fragment —
    which is what keeps the operator pointwise."""
    compiled = compiler().expression(plan.Sum(plan.Variable('p'), ('generator',)), 'test')
    fragment = compiled.terms[0]
    assert fragment.dims == ('snapshot',)
    assert columns(fragment.frame) == ['snapshot', 'var_label', 'coeff']
    assert 'AGGREGATE' not in query(fragment.frame)


def test_sum_over_an_absent_dim_scales_by_that_dims_cardinality():
    """Eager parity: summing a snapshot-only term over `generator` repeats it."""
    inner = plan.Sum(plan.Variable('p'), ('generator',))
    compiled = compiler().expression(plan.Sum(inner, ('generator',)), 'test')
    assert '3' in query(compiled.terms[0].frame)


def test_group_sum_swaps_the_source_dim_for_the_target_and_emits_no_aggregate():
    node = plan.GroupSum(plan.Variable('p'), over='generator', coordinate='bus', into='bus')
    fragment = compiler().expression(node, 'test').terms[0]
    assert fragment.dims == ('snapshot', 'bus')
    assert columns(fragment.frame) == ['snapshot', 'bus', 'var_label', 'coeff']
    assert 'AGGREGATE' not in query(fragment.frame)
    assert joins(fragment.frame) == 1


def test_translate_keeps_its_dims_and_joins_the_dim_table_twice():
    """Bounded halo: a row at ord *o* lands at ord *o + by*, no window."""
    fragment = compiler().expression(plan.Translate(plan.Variable('p'), 'snapshot', by=1), 'test').terms[0]
    assert fragment.dims == ('snapshot', 'generator')
    assert columns(fragment.frame) == ['generator', 'snapshot', 'var_label', 'coeff']
    assert joins(fragment.frame) == 2
    assert 'OVER' not in query(fragment.frame)


def test_wrapping_is_modulo_and_acyclic_is_not():
    cyclic = compiler().expression(plan.Translate(plan.Variable('p'), 'snapshot', by=1, wrap=True), 't').terms[0]
    acyclic = compiler().expression(plan.Translate(plan.Variable('p'), 'snapshot', by=1, wrap=False), 't').terms[0]
    assert '%' in query(cyclic.frame)
    assert '%' not in query(acyclic.frame)


def test_a_shape_operator_along_a_dim_the_expression_lacks_is_refused():
    with pytest.raises(LanguageError, match='translation'):
        compiler().expression(plan.Translate(plan.Parameter('cost'), 'snapshot', by=1), 'test')


# ---------------------------------------------------------------------------
# predicates
# ---------------------------------------------------------------------------


def test_a_dimension_comparison_filters_a_column_already_in_the_frame():
    """Pointwise, and free: no table is read to decide it."""
    frame = compiler().frame(('snapshot',), plan.DimensionComparison('snapshot', '>', 0))
    text = query(frame)
    assert 'FILTER' in text
    assert 'JOIN' not in text


def test_a_parameter_predicate_needs_a_left_join():
    frame = compiler().frame(('generator',), plan.ParameterDefined('available'))
    text = query(frame)
    assert 'JOIN' in text
    assert 'FILTER' in text


def test_defined_on_a_boolean_parameter_tests_the_value_not_its_finiteness():
    numeric = query(compiler().frame(('generator',), plan.ParameterDefined('available')))
    boolean = query(compiler(frozenset({'available'})).frame(('generator',), plan.ParameterDefined('available')))
    assert 'is_finite' in numeric
    assert 'is_finite' not in boolean


def test_a_where_parameter_outside_the_frame_dims_is_refused():
    """Otherwise the mask would be reduced over a dim the declaration never named."""
    with pytest.raises(LanguageError, match='outside the foreach dims'):
        compiler().frame(('generator',), plan.ParameterDefined('load'))


# ---------------------------------------------------------------------------
# frames and bounds
# ---------------------------------------------------------------------------


def test_a_frame_cross_joins_its_dim_tables_and_carries_their_ordinals():
    frame = compiler().frame(('snapshot', 'generator'), None)
    assert columns(frame) == ['snapshot', '__ord snapshot__', 'generator', '__ord generator__']
    assert 'CROSS' in query(frame)


def test_an_unmasked_frame_has_nothing_to_filter():
    assert 'FILTER' not in query(compiler().frame(('snapshot', 'generator'), None))


def test_a_parameter_bound_joins_on_the_variable_frame():
    variable = plan.VariableDeclaration('p', ('snapshot', 'generator'), upper=plan.Parameter('cost'))
    bounded = compiler().bounds(VARIABLES['p'], variable)
    assert {'lb', 'ub'} <= set(columns(bounded))
    assert joins(bounded) == 1


def test_a_constant_bound_needs_no_join_at_all():
    bounded = compiler().bounds(VARIABLES['p'], PROGRAM.variables[0])
    assert {'lb', 'ub'} <= set(columns(bounded))
    assert joins(bounded) == 0


def test_a_bound_carrying_a_variable_is_refused():
    variable = plan.VariableDeclaration('p', ('snapshot', 'generator'), upper=plan.Variable('p'))
    with pytest.raises(LanguageError, match='bounds must be variable-free'):
        compiler().bounds(VARIABLES['p'], variable)


# ---------------------------------------------------------------------------
# keyed — what lets the assembly skip its terminal aggregate
# ---------------------------------------------------------------------------


def test_a_variable_and_its_shape_operators_stay_keyed():
    """A term keeps one row per (dims…, var_label) through every operator.

    `Sum` drops a coordinate column, but the rows it merges came from distinct
    coordinates and so carry distinct labels; `group_sum` and `roll` join a dim
    table one-to-one. None of them can put a variable on a row twice.
    """
    for node in (
        plan.Variable('p'),
        plan.Sum(plan.Variable('p'), ('generator',)),
        plan.GroupSum(plan.Variable('p'), over='generator', coordinate='bus', into='bus'),
        plan.Translate(plan.Variable('p'), 'snapshot', by=1),
        plan.Multiply(plan.Variable('p'), plan.Parameter('cost')),
        -plan.Variable('p'),
    ):
        assert compiler().expression(node, 'test').terms[0].keyed, node


def test_summing_a_constant_part_over_a_dim_stops_it_being_keyed():
    """The exception, and the reason the flag is not just "is it a term".

    Dropping a dim from a constant part genuinely merges rows — there is no
    label left to tell them apart — so a term multiplied by one inherits that.
    """
    summed = plan.Sum(plan.Parameter('cost'), ('generator',))
    assert not compiler().expression(summed, 'test').consts[0].keyed
    product = plan.Multiply(plan.Variable('p'), summed)
    assert not compiler().expression(product, 'test').terms[0].keyed


def test_a_sum_that_drops_nothing_leaves_a_constant_part_keyed():
    """Summing over a dim the operand lacks scales it; no rows merge."""
    scaled = plan.Sum(plan.Multiply(plan.Variable('p'), plan.Parameter('cost')), ('snapshot',))
    assert compiler().expression(scaled, 'test').terms[0].keyed


def test_group_sum_over_a_broadcast_dim_is_not_keyed():
    """Which dim is grouped decides whether the key survives.

    `generator` is `p`'s own, so grouping it merges rows with distinct labels.
    Reaching the fragment by broadcast — a parameter carrying a dim the
    variable does not — merges rows carrying the same one, and nothing
    downstream can tell those apart.
    """
    own = plan.GroupSum(plan.Variable('p'), over='generator', coordinate='bus', into='bus')
    assert compiler().expression(own, 'test').terms[0].keyed

    broadcast = plan.GroupSum(
        plan.Multiply(plan.Variable('p'), plan.Parameter('cost')),
        over='generator',
        coordinate='bus',
        into='bus',
    )
    assert compiler().expression(broadcast, 'test').terms[0].keyed  # still p's own dim

    lone = plan.Program(
        parameters=PROGRAM.parameters,
        variables=(plan.VariableDeclaration('q', ('snapshot',)),),
        constraints=(),
        objective=plan.ObjectiveDeclaration('min', plan.Variable('q')),
        dimensions=PROGRAM.dimensions,
    )
    lonely = PolarsCompiler(
        lone,
        CARDINALITY,
        frozenset(),
        PARAMETERS,
        DIMENSIONS,
        {'q': pl.LazyFrame(schema={'snapshot': pl.Int64, 'var_label': pl.Int64})},
    )
    node = plan.GroupSum(
        plan.Multiply(plan.Variable('q'), plan.Parameter('cost')),
        over='generator',
        coordinate='bus',
        into='bus',
    )
    assert not lonely.expression(node, 'test').terms[0].keyed
