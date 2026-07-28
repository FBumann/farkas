"""The LaTeX renderer (spike).

Assertions are on *fragments*, not whole documents: a golden file for a
generator this young would be rewritten by every cosmetic change and would
stop being read. What is pinned here is what the rendering has to *mean* —
which index a reduction binds, which side of a translation the offset lands
on, where a mask goes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import farkas as fk
from farkas.latex import to_latex

DISPATCH = {
    'dimensions': {'snapshot': {'dtype': 'int'}, 'generator': {'dtype': 'str'}},
    'parameters': {
        'p_max': {'dims': ['generator']},
        'cost': {'dims': ['generator']},
        'load': {'dims': ['snapshot']},
    },
    'variables': {'p': {'foreach': ['snapshot', 'generator'], 'bounds': {'lower': 0, 'upper': 'p_max'}}},
    'constraints': {
        'power_balance': {
            'foreach': ['snapshot'],
            'equations': [{'expression': 'sum(p, over=generator) == load'}],
        }
    },
    'objectives': {'total_cost': {'sense': 'minimize', 'equations': [{'expression': 'p * cost'}]}},
}


def test_symbols_follow_the_names():
    """A single-letter name stays a letter; a longer one is set in \\mathit;
    the tail of an underscored name becomes a superscript, because the
    subscript slot is spoken for by the dimensions."""
    tex = to_latex(DISPATCH)
    assert 'p_{t,g}' in tex
    assert r'\mathit{load}_{t}' in tex
    assert r'p^{\mathrm{max}}_{g}' in tex


def test_sum_binds_the_dimension_it_reduces():
    tex = to_latex(DISPATCH, legend=False)
    assert r'\sum_{g \in \mathcal{G}} p_{t,g} & = \mathit{load}_{t}' in tex


def test_constraint_carries_its_foreach_as_a_quantifier():
    assert r'\forall\, t \in \mathcal{T}' in to_latex(DISPATCH)


def test_objective_sums_over_every_dim_its_term_carries():
    """The declaration implies the reduction; the rendering says it out loud."""
    tex = to_latex(DISPATCH, legend=False)
    assert r'\min \quad & \sum_{t \in \mathcal{T},\ g \in \mathcal{G}} p_{t,g} \cdot \mathit{cost}_{g}' in tex


def test_bounds_become_a_domain_line():
    assert r'0 \le p_{t,g} & \le p^{\mathrm{max}}_{g}' in to_latex(DISPATCH)


@pytest.mark.parametrize(
    ('bounds', 'expected'),
    [
        ({}, r'p_{t,g} & \in \mathbb{R}'),
        ({'lower': 0}, r'p_{t,g} & \ge 0'),
        ({'upper': 10}, r'p_{t,g} & \le 10'),
    ],
)
def test_a_missing_bound_is_not_silently_zero(bounds: dict[str, object], expected: str):
    model = {**DISPATCH, 'variables': {'p': {'foreach': ['snapshot', 'generator'], 'bounds': bounds}}}
    assert expected in to_latex(model)


def test_binary_and_integer_variables_state_their_domain():
    model = {
        **DISPATCH,
        'variables': {
            'p': {'foreach': ['snapshot', 'generator'], 'bounds': {'lower': 0, 'upper': 'p_max'}},
            'on': {'foreach': ['snapshot', 'generator'], 'binary': True},
            'n': {'foreach': ['generator'], 'integer': True, 'bounds': {'lower': 0, 'upper': 5}},
        },
    }
    tex = to_latex(model)
    assert r'\{0, 1\}' in tex
    assert r'\in \mathbb{Z}' in tex


def test_a_where_lands_on_the_quantifier_not_in_the_equation():
    """A mask is row absence, so it belongs to the ∀ that names the rows."""
    model = {
        **DISPATCH,
        'variables': {
            'p': {
                'foreach': ['snapshot', 'generator'],
                'where': 'p_max > 0',
                'bounds': {'lower': 0, 'upper': 'p_max'},
            }
        },
    }
    tex = to_latex(model, legend=False)
    assert r'\forall\, t \in \mathcal{T},\ g \in \mathcal{G} \,:\, p^{\mathrm{max}}_{g} > 0' in tex


def test_where_operators_are_logical_symbols():
    model = {
        **DISPATCH,
        'parameters': {**DISPATCH['parameters'], 'flag': {'dims': ['generator'], 'dtype': 'bool'}},
        'variables': {
            'p': {
                'foreach': ['snapshot', 'generator'],
                'where': 'p_max > 0 AND NOT flag',
                'bounds': {'lower': 0, 'upper': 'p_max'},
            }
        },
    }
    tex = to_latex(model, legend=False)
    assert r'\wedge' in tex
    assert r'\neg' in tex


def test_translation_shows_at_the_leaf_and_distinguishes_roll_from_shift():
    """``roll`` wraps and ``shift`` does not — one symbol each, since a reader
    who cannot tell them apart cannot tell the two models apart either."""

    def storage(helper: str) -> dict[str, object]:
        return {
            'dimensions': {'snapshot': {'dtype': 'int'}},
            'parameters': {'load': {'dims': ['snapshot']}},
            'variables': {'soc': {'foreach': ['snapshot'], 'bounds': {'lower': 0, 'upper': 100}}},
            'constraints': {
                'balance': {
                    'foreach': ['snapshot'],
                    'equations': [{'expression': f'soc == {helper}(soc, snapshot=1) + load'}],
                }
            },
        }

    assert r'\mathit{soc}_{t \ominus 1}' in to_latex(storage('roll'), legend=False)
    assert r'\mathit{soc}_{t - 1}' in to_latex(storage('shift'), legend=False)


def test_the_legend_explains_wraparound_only_when_it_is_used():
    tex_with_roll = to_latex(
        {
            'dimensions': {'snapshot': {'dtype': 'int'}},
            'variables': {'soc': {'foreach': ['snapshot'], 'bounds': {'lower': 0}}},
            'constraints': {
                'b': {'foreach': ['snapshot'], 'equations': [{'expression': 'soc == roll(soc, snapshot=1)'}]}
            },
        }
    )
    assert r'\ominus' in tex_with_roll
    assert 'cyclic translation' in tex_with_roll
    assert 'cyclic translation' not in to_latex(DISPATCH)


def test_group_sum_renders_the_coordinate_map_as_a_set_condition():
    tex = to_latex('examples/transport.yaml', legend=False)
    assert r'\sum_{g \in \mathcal{G} \,:\, \mathrm{bus}(g) = b} p_{t,g}' in tex
    assert r'\sum_{l \in \mathcal{L} \,:\, \mathrm{to}(l) = b} f_{t,l}' in tex


def test_a_sum_used_as_a_factor_is_bracketed():
    """Unbracketed, ``\\sum_g x_g \\cdot y`` reads as the sum capturing ``y``."""
    model = {
        **DISPATCH,
        'constraints': {
            'power_balance': {
                'foreach': ['snapshot'],
                'equations': [{'expression': 'sum(p, over=generator) * 2 == load'}],
            }
        },
    }
    tex = to_latex(model, legend=False)
    assert r'\left( \sum_{g \in \mathcal{G}} p_{t,g} \right) \cdot 2' in tex


def test_an_additive_sum_body_is_bracketed_but_a_nested_reduction_is_not():
    r"""``\sum_t \sum_g x`` is unambiguous; ``\sum_t a + b`` is not."""
    nested = to_latex('examples/walkthrough.yaml', legend=False)
    assert r'\sum_{t \in \mathcal{T}} \sum_{g \in \mathcal{G}} p_{t,g} \cdot \mathit{cost}_{g}' in nested

    additive = to_latex(
        {
            **DISPATCH,
            'constraints': {
                'power_balance': {
                    'foreach': ['snapshot'],
                    'equations': [{'expression': 'sum(p + p, over=generator) == load'}],
                }
            },
        },
        legend=False,
    )
    assert r'\sum_{g \in \mathcal{G}} \left( p_{t,g} + p_{t,g} \right)' in additive


def test_macros_and_named_expressions_are_expanded_away():
    """Rendering happens on core AST, like both lanes — what prints is the
    math a backend builds, not the sugar the file spells it with."""
    model = {
        **DISPATCH,
        'expressions': {'supply': 'sum(p, over=generator)'},
        'constraints': {'power_balance': {'foreach': ['snapshot'], 'equations': [{'expression': 'supply == load'}]}},
    }
    tex = to_latex(model, legend=False)
    assert r'\sum_{g \in \mathcal{G}} p_{t,g}' in tex
    assert 'supply' not in tex


def test_piecewise_prints_the_formulation_it_expands_to():
    tex = to_latex('examples/piecewise.yaml', legend=False)
    assert 'cost\\_curve\\_convexity' in tex
    # Subscript order is not asserted: `piecewise._validate_block` builds the
    # emitted frame by iterating a frozenset, so the generated variable's dim
    # order varies with PYTHONHASHSEED. That is a bug in the expansion, not in
    # the rendering — pin the order here once it is fixed.
    assert r'\mathit{cost}^{\mathrm{curve,lam}}_{' in tex
    assert r'\sum_{b \in \mathcal{B}}' in tex


def test_names_are_escaped_in_text_mode():
    assert r'\text{power\_balance}' in to_latex(DISPATCH)


def test_every_example_renders():
    """The renderer walks the same AST as lowering, so anything ``check``
    accepts it must be able to print — a node it forgets is an exception, not
    a blank."""
    for path in _examples():
        assert to_latex(path).strip()


def _examples() -> list[Path]:
    return sorted(Path('examples').glob('*.yaml'))


def _structural_errors(tex: str) -> list[str]:
    """The three ways generated LaTeX usually fails to compile.

    Not a substitute for running TeX — it cannot know whether ``\\mathcal``
    takes an argument — but brace balance, environment nesting and
    ``\\left``/``\\right`` pairing are exactly what a *generator* gets wrong,
    and they are checkable without a toolchain nobody has in CI.
    """
    errors = []
    depth = 0
    for i, c in enumerate(tex):
        escaped = i > 0 and tex[i - 1] == '\\'
        if c == '{' and not escaped:
            depth += 1
        elif c == '}' and not escaped:
            depth -= 1
            if depth < 0:
                errors.append(f'unbalanced closing brace at offset {i}')
                break
    if depth > 0:
        errors.append(f'{depth} unclosed brace(s)')

    stack: list[str] = []
    for verb, environment in re.findall(r'\\(begin|end)\{(\w+\*?)\}', tex):
        if verb == 'begin':
            stack.append(environment)
        elif not stack:
            errors.append(rf'\end{{{environment}}} with nothing open')
        elif stack.pop() != environment:
            errors.append(rf'\end{{{environment}}} does not close the open environment')
    if stack:
        errors.append(f'environments left open: {stack}')

    left, right = tex.count(r'\left'), tex.count(r'\right')
    if left != right:
        errors.append(rf'\left/\right mismatch: {left} vs {right}')
    return errors


@pytest.mark.parametrize('path', _examples(), ids=lambda p: p.stem)
def test_the_output_is_structurally_well_formed(path: Path):
    assert _structural_errors(to_latex(path, standalone=True)) == []


def test_standalone_is_a_whole_document():
    tex = to_latex(DISPATCH, standalone=True)
    assert tex.startswith(r'\documentclass')
    assert r'\usepackage{amsmath}' in tex
    assert tex.rstrip().endswith(r'\end{document}')


def test_numbering_can_be_turned_off():
    assert r'\begin{align*}' in to_latex(DISPATCH, numbered=False)
    assert r'\begin{align}' in to_latex(DISPATCH, numbered=True)


def test_an_invalid_model_fails_the_same_way_check_does():
    broken = {**DISPATCH, 'objectives': {'total_cost': {'equations': [{'expression': 'p * nonexistent'}]}}}
    with pytest.raises(fk.LinopyYamlError):
        to_latex(broken)


def test_exported_from_the_package():
    assert fk.to_latex is to_latex
