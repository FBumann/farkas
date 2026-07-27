"""The streaming language boundary: out-of-subset constructs are load errors.

There is no runtime fallback — the streaming subset IS the language
(ARCHITECTURE.md). The eager builder survives only as the opt-in
compatibility layer (`farkas.linopy`) and the differential oracle.
Errors must carry the construct and its context, verbatim.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from farkas.errors import LanguageError
from farkas.lowering import lower_program
from tests.conftest import schema_of

DISPATCH = Path('examples/dispatch.yaml')


def _objective(expression: str) -> dict:
    return {'objectives.total_cost.equations': [{'expression': expression}]}


@pytest.mark.parametrize('path', sorted(Path('examples').glob('*.yaml')), ids=lambda p: p.name)
def test_every_shipped_example_is_inside_the_language(path):
    """The examples are the language's own claim about itself — one of them
    falling outside the streaming subset would be a documentation bug that
    only shows up when a reader runs it."""
    lower_program(schema_of(path))


@pytest.mark.parametrize(
    'patch',
    [
        pytest.param({'variables.p.binary': True, 'variables.p.bounds': {}}, id='binary-variable'),
        # ROADMAP 5b: both lanes accept `where: "snapshot > 2"`
        pytest.param({'variables.p.where': 'snapshot > 2'}, id='where-on-a-dimension'),
        pytest.param(_objective('sum(p * cost, over=generator)'), id='affine-product'),
    ],
)
def test_inside_the_language(patch):
    lower_program(schema_of(DISPATCH, **patch))  # must not raise


@pytest.mark.parametrize(
    ('patch', 'match'),
    [
        pytest.param(
            {'constraints.power_balance.equations': [{'expression': 'sum(p ** 2, over=generator) == load'}]},
            r"operator '\*\*'",
            id='power-operator',
        ),
        # `check()` must catch degree 2 — it is the first clause of the ceiling.
        # The affine guard used to live only in the executor, so it needed data
        # bound: `fk.check()` accepted this and the model only blew up at build
        # time, which made check() useless as the CI verb for exactly the rule
        # it should enforce first.
        pytest.param(_objective('sum(p * p, over=generator)'), 'degree 2', id='degree-two'),
        pytest.param(_objective('sum(cost / p, over=generator)'), 'divisor contains variables', id='variable-divisor'),
    ],
)
def test_outside_the_language_is_a_load_error(patch, match):
    with pytest.raises(LanguageError, match=match):
        lower_program(schema_of(DISPATCH, **patch))


def test_an_unknown_helper_names_its_context_and_teaches_the_rewrite():
    """The message is the whole test: an error that pointed at another lane
    would be telling the user to leave the language rather than restate it."""
    patch = {'constraints.power_balance.equations': [{'expression': 'my_helper(p, over=generator) == load'}]}
    with pytest.raises(LanguageError, match='my_helper') as exc:
        lower_program(schema_of(DISPATCH, **patch))

    reason = str(exc.value)
    assert 'power_balance' in reason  # reason carries context
    assert 'escape' in reason  # ...and the rewrite, not a pointer to another lane
    assert 'eager' not in reason.lower()
