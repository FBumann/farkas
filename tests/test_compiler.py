"""The compiler is pure, and this file is the proof.

No duckdb, no highspy, no data — a plan node goes in, SQL text comes out.
That is the seam the split bought: before it, checking what SQL an operator
emits meant building a model and solving it.

These assertions are deliberately about *shape*, not exact text. They pin the
properties ARCHITECTURE.md's admissibility test reads off the SQL — which dim
columns survive, whether an aggregate or a window appears, whether a mask
becomes a join or a filter — and leave formatting free to change.
"""

from __future__ import annotations

import pytest

from farkas.errors import LanguageError
from farkas.relational import plan
from farkas.relational.compiler import SqlCompiler

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


def compiler(boolean_parameters: frozenset[str] = frozenset()) -> SqlCompiler:
    return SqlCompiler(PROGRAM, CARDINALITY, boolean_parameters)


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
    assert 'FROM var_p' in fragment.sql


def test_a_parameter_is_a_constant_fragment_not_a_term():
    compiled = compiler().expression(plan.Parameter('cost'), 'test')
    assert not compiled.terms
    assert compiled.consts[0].dims == ('generator',)
    assert not compiled.consts[0].is_term


def test_addition_concatenates_fragments_rather_than_joining():
    """An LP row is a sum of terms, so ``+`` needs no SQL at all."""
    compiled = compiler().expression(plan.Variable('p') + plan.Variable('p'), 'test')
    assert len(compiled.terms) == 2


def test_a_product_of_two_variable_carrying_factors_is_refused():
    with pytest.raises(LanguageError, match='nonlinear product'):
        compiler().expression(plan.Multiply(plan.Variable('p'), plan.Variable('p')), 'test')


def test_a_divisor_carrying_variables_is_refused():
    with pytest.raises(LanguageError, match='nonlinear quotient'):
        compiler().expression(plan.Divide(plan.Variable('p'), plan.Variable('p')), 'test')


# ---------------------------------------------------------------------------
# shape operators — each rewrites exactly one dim column
# ---------------------------------------------------------------------------


def test_sum_drops_the_dim_it_sums_over():
    compiled = compiler().expression(plan.Sum(plan.Variable('p'), ('generator',)), 'test')
    assert compiled.terms[0].dims == ('snapshot',)


def test_sum_over_an_absent_dim_scales_by_that_dims_cardinality():
    """Eager parity: summing a snapshot-only term over `generator` repeats it."""
    inner = plan.Sum(plan.Variable('p'), ('generator',))
    compiled = compiler().expression(plan.Sum(inner, ('generator',)), 'test')
    assert 'coeff * 3' in compiled.terms[0].sql


def test_group_sum_swaps_the_source_dim_for_the_target_and_emits_no_aggregate():
    """The GROUP BY lives in the terminal assembly, not in the fragment —
    which is what keeps the operator pointwise."""
    node = plan.GroupSum(plan.Variable('p'), over='generator', coordinate='bus', into='bus')
    fragment = compiler().expression(node, 'test').terms[0]
    assert fragment.dims == ('snapshot', 'bus')
    assert 'GROUP BY' not in fragment.sql
    assert 'JOIN dim_generator' in fragment.sql


def test_translate_keeps_its_dims_and_joins_the_dim_table_twice():
    """Bounded halo: a row at ord *o* lands at ord *o + by*, no window."""
    fragment = compiler().expression(plan.Translate(plan.Variable('p'), 'snapshot', by=1), 'test').terms[0]
    assert fragment.dims == ('snapshot', 'generator')
    assert fragment.sql.count('JOIN dim_snapshot') == 2
    assert 'OVER (' not in fragment.sql


def test_wrapping_is_modulo_and_acyclic_is_not():
    cyclic = compiler().expression(plan.Translate(plan.Variable('p'), 'snapshot', by=1, wrap=True), 't').terms[0]
    acyclic = compiler().expression(plan.Translate(plan.Variable('p'), 'snapshot', by=1, wrap=False), 't').terms[0]
    assert '% 24' in cyclic.sql
    assert '%' not in acyclic.sql


def test_a_shape_operator_along_a_dim_the_expression_lacks_is_refused():
    with pytest.raises(LanguageError, match='translation'):
        compiler().expression(plan.Translate(plan.Parameter('cost'), 'snapshot', by=1), 'test')


# ---------------------------------------------------------------------------
# predicates
# ---------------------------------------------------------------------------


def test_a_dimension_comparison_filters_a_column_already_in_the_frame():
    joins, condition = compiler().predicate(plan.DimensionComparison('snapshot', '>', 0), ('snapshot',))
    assert joins == []
    assert 't_snapshot.val > 0' in condition


def test_a_parameter_predicate_needs_a_left_join():
    joins, condition = compiler().predicate(plan.ParameterDefined('available'), ('generator',))
    assert len(joins) == 1
    assert 'LEFT JOIN p_available' in joins[0]
    assert 'isfinite' in condition


def test_defined_on_a_boolean_parameter_tests_the_value_not_its_finiteness():
    _, condition = compiler(frozenset({'available'})).predicate(plan.ParameterDefined('available'), ('generator',))
    assert 'isfinite' not in condition


def test_a_mask_is_wrapped_so_a_null_excludes_the_row():
    _, condition = compiler().predicate(plan.ParameterComparison('available', '>', 0), ('generator',))
    assert condition.startswith('COALESCE(')
    assert condition.endswith(', FALSE)')


def test_a_where_parameter_outside_the_frame_dims_is_refused():
    """Otherwise the mask would be reduced over a dim the declaration never named."""
    with pytest.raises(LanguageError, match='outside the foreach dims'):
        compiler().predicate(plan.ParameterDefined('load'), ('generator',))


# ---------------------------------------------------------------------------
# frames and bounds
# ---------------------------------------------------------------------------


def test_a_frame_cross_joins_its_dim_tables_and_orders_by_ordinal():
    from_clause, where_clause, order_key = compiler().frame(('snapshot', 'generator'), None)
    assert from_clause == 'dim_snapshot t_snapshot CROSS JOIN dim_generator t_generator'
    assert where_clause == 'TRUE'
    assert order_key == 't_snapshot.ord, t_generator.ord'


def test_a_positional_label_is_row_major_arithmetic_on_the_ordinals():
    """No window, no sort — the trailing dim has stride 1 and is left bare."""
    sql = compiler().positional_label(('snapshot', 'generator'), 0)
    assert sql == '(t_snapshot.ord * 3 + t_generator.ord)::BIGINT'
    assert 'OVER (' not in sql


def test_a_positional_label_carries_the_running_offset():
    assert compiler().positional_label(('generator',), 72) == '(t_generator.ord + 72)::BIGINT'


def test_a_positional_label_agrees_with_the_window_it_replaces():
    """The two paths assign solver indices, so agreeing 'in spirit' is not
    enough — evaluate the arithmetic against the order key's enumeration."""
    dims = ('snapshot', 'generator', 'bus')
    sql = compiler().positional_label(dims, 5)
    ordinals = [
        (s, g, b)
        for s in range(CARDINALITY['snapshot'])
        for g in range(CARDINALITY['generator'])
        for b in range(CARDINALITY['bus'])
    ]
    for expected, (s, g, b) in enumerate(ordinals, start=5):
        expression = sql.removesuffix('::BIGINT').replace('t_snapshot.ord', str(s))
        expression = expression.replace('t_generator.ord', str(g)).replace('t_bus.ord', str(b))
        # `+` and `*` on integer literals mean the same thing in both languages,
        # so python evaluates the emitted SQL faithfully — and this file has no
        # duckdb to ask instead
        assert eval(expression) == expected


def test_a_parameter_bound_joins_on_the_variable_frame():
    variable = PROGRAM.variables[0]
    sql, joins = compiler().bound(plan.Parameter('cost'), variable)
    assert sql == 'b_cost.value'
    assert 'LEFT JOIN p_cost b_cost ON b_cost.generator = f.generator' in joins[0]


def test_a_bound_carrying_a_variable_is_refused():
    with pytest.raises(LanguageError, match='bounds must be variable-free'):
        compiler().bound(plan.Variable('p'), PROGRAM.variables[0])
