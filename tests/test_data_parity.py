"""Malformed *data*, checked for the same verdict on both lanes.

`test_resolution_parity.py` does this for the language: the same YAML is
accepted or refused identically, because hard rule 3 says both lanes accept
exactly the same language. Nothing said the same about the same **data**, and
the checks are written twice — 15 `DataError` sites in `relational/executor.py`
against 16 in `linopy/loader.py`, with only the wording of two of them shared
(#351). Two of the six cases below diverged when this table was written:

- an unknown label was **accepted** by the relational lane, worth two thirds of
  the objective on the model here (#350);
- a duplicated coordinate row raised `DataError` relationally and a bare
  `ValueError` eagerly — which SPEC §9 names as the failure mode to avoid, "an
  opaque xarray or solver exception with no pointer back to a YAML
  declaration".

The table is the contract the decoupling in #351 has to preserve. It is also
what makes "decoupled" mean something: without it, the next divergence lands
the way these did — silently, and found by accident.

**Each case carries data twice**, once per lane's preferred shape. That is not
duplication for its own sake: the relational lane adapts everything to tidy
polars frames, the eager lane reads pandas/xarray natively because that is what
linopy wants, and the point of the table is that two *representations* of the
same mistake get the same answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
import yaml as pyyaml

import lpspec as lps
from lpspec.errors import DataError
from tests.oracle import lpspec_linopy, pd  # skips the module without the [linopy] extra

if TYPE_CHECKING:
    from pathlib import Path

#: One dimension, one variable, a coefficient and a bound — the smallest model
#: that has somewhere for each kind of bad data to go wrong.
MODEL = {
    'dimensions': {'f': {'values': ['a', 'b']}},
    'parameters': {'cost': {'dims': ['f']}, 'cap': {'dims': ['f']}},
    'variables': {'x': {'foreach': ['f'], 'bounds': {'lower': 0, 'upper': 'cap'}}},
    'constraints': {'k': {'foreach': ['f'], 'expression': 'x <= cap'}},
    'objectives': {'o': {'sense': 'maximize', 'expression': 'x * cost'}},
}

ACCEPTED = 'accepted'


def _tidy(**cols: list[Any]) -> pl.DataFrame:
    return pl.DataFrame(cols)


@dataclass(frozen=True)
class Case:
    """One malformed (or valid) binding, in both representations."""

    label: str
    relational: dict[str, Any]
    eager: dict[str, Any]
    #: `DataError` for a refusal both lanes owe the caller, or `ACCEPTED`.
    verdict: type[Exception] | str


def _cases() -> list[Case]:
    good_r = {'cost': _tidy(f=['a', 'b'], value=[1.0, 2.0]), 'cap': _tidy(f=['a', 'b'], value=[5.0, 5.0])}
    good_e = {'cost': pd.Series({'a': 1.0, 'b': 2.0}), 'cap': pd.Series({'a': 5.0, 'b': 5.0})}
    return [
        Case('valid', good_r, good_e, ACCEPTED),
        Case(
            'parameter missing entirely',
            {'cost': good_r['cost']},
            {'cost': good_e['cost']},
            DataError,
        ),
        Case(
            'bound parameter sparse',
            {**good_r, 'cap': _tidy(f=['a'], value=[5.0])},
            {**good_e, 'cap': pd.Series({'a': 5.0})},
            # No reading of a missing bound contributes nothing, so law 8
            # refuses rather than guessing (`null_bounds_message`).
            DataError,
        ),
        Case(
            'coefficient sparse',
            {**good_r, 'cost': _tidy(f=['a'], value=[1.0])},
            {**good_e, 'cost': pd.Series({'a': 1.0})},
            # The ordinary case: a missing row is a zero coefficient (§8).
            ACCEPTED,
        ),
        Case(
            'duplicated coordinate row',
            {**good_r, 'cost': _tidy(f=['a', 'a', 'b'], value=[1.0, 9.0, 2.0])},
            {**good_e, 'cost': pd.Series([1.0, 9.0, 2.0], index=pd.Index(['a', 'a', 'b'], name='f'))},
            # Which value applies is undefined, so neither lane may pick one.
            DataError,
        ),
        Case(
            'label the dimension does not have',
            {**good_r, 'cost': _tidy(f=['a', 'zz'], value=[1.0, 2.0])},
            {**good_e, 'cost': pd.Series({'a': 1.0, 'zz': 2.0})},
            # Present and unaddressable is a typo, not sparsity (#350).
            DataError,
        ),
    ]


CASES = _cases()


@pytest.fixture(scope='module')
def model_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The eager lane only takes a path, so the model has to hit disk once."""
    path = tmp_path_factory.mktemp('data-parity') / 'm.yaml'
    path.write_text(pyyaml.safe_dump(MODEL))
    return path


def _verdict_relational(path: Path, data: dict[str, Any]) -> type[Exception] | str:
    try:
        lps.solve(path, data)
    except DataError:
        return DataError
    return ACCEPTED


def _verdict_eager(path: Path, data: dict[str, Any]) -> type[Exception] | str:
    try:
        m = lpspec_linopy.build(path, data=data)
        m.solve(solver_name='highs', output_flag=False)
    except DataError:
        return DataError
    return ACCEPTED


@pytest.mark.parametrize('case', CASES, ids=lambda c: c.label)
def test_both_lanes_reach_the_same_verdict(case: Case, model_path: Path):
    """And it is the verdict the table names, not merely the same one.

    Asserting agreement alone would pass on two lanes that are both wrong,
    which is the failure this table exists to catch rather than to reproduce.
    """
    relational = _verdict_relational(model_path, case.relational)
    eager = _verdict_eager(model_path, case.eager)

    assert relational == case.verdict, f'{case.label}: relational lane'
    assert eager == case.verdict, f'{case.label}: eager lane'


def test_the_table_covers_both_verdicts():
    """A guard on the guard: a table that had drifted to all-accepted, or to
    all-refused, would still pass every assertion above.
    """
    verdicts = {c.verdict for c in CASES}
    assert verdicts == {ACCEPTED, DataError}, f'expected both verdicts to be exercised; got {verdicts}'
