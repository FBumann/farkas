"""Ported models, checked against an optimum that did not come from us.

Every other test here compares farkas against farkas. Even the differential
harness compares two lanes consuming the *same resolved AST* (hard rule 1), so
a **shared misreading** — both lanes agreeing on a meaning the modeller did not
intend — passes the whole suite green. This is the net for that class.

Each expected objective was published with the model or produced by somebody
else's code; ``examples/ports/references/`` holds the scripts, run out of band,
and ``references.json`` records what they said. So the corpus needs no oracle
and no extra dependency: it is linopy-free and pandas-free, and runs on the
bare-install job. See docs/models/index.md; the gallery page for each port is
asserted against its model file by ``test_models_gallery.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl
import pytest

import farkas as fk

PORTS = Path(__file__).resolve().parent.parent / 'examples' / 'ports'
REFERENCES: dict[str, dict[str, Any]] = json.loads((PORTS / 'references.json').read_text())


def sources(name: str) -> dict[str, Any]:
    """One JSON per port: a column-oriented table per name, scalars inline."""
    data = json.loads((PORTS / 'data' / f'{name}.json').read_text())
    return {k: pl.DataFrame(v) if isinstance(v, dict) else v for k, v in data.items()}


@pytest.fixture(params=sorted(REFERENCES), ids=str)
def port(request: pytest.FixtureRequest) -> dict[str, Any]:
    return {'name': request.param, 'model': PORTS / f'{request.param}.yaml'} | REFERENCES[request.param]


def test_port_reaches_the_reference_optimum(port: dict[str, Any]) -> None:
    """The objective, never the primal — ``transport_dantzig`` reaches 153.675
    at a different vertex than the source prints, so a corpus pinned to a
    solution would fail on a solver upgrade that broke nothing. ``rtol`` is per
    port because a published optimum is rounded and a solved one is not."""
    with fk.solve(port['model'], sources(port['name'])) as solution:
        assert solution.is_ok, f'{port["name"]} did not solve: {solution.status}'
        assert solution.objective == pytest.approx(port['objective'], rel=port['rtol']), (
            f'{port["name"]} disagrees with {port["provenance"]}'
        )


def test_port_is_inside_the_language(port: dict[str, Any]) -> None:
    """Compiles with no data bound, so a language regression fails separately
    from a semantics one: this breaks when lowering stops accepting the model,
    the test above when it lowers and misses the number."""
    fk.check(port['model'])


def test_port_reaches_the_reference_duals(port: dict[str, Any]) -> None:
    """The shadow prices, against the same outside implementation.

    An objective is one number and it hides a great deal. A dual vector is the
    output this audience actually reads — PyPSA's ``marginal_price`` is the
    nodal price — and it is where two implementations most reliably disagree
    quietly: which side of the constraint the price belongs to, and what sign
    an inequality's carries. ``transport_dantzig`` is here for exactly that,
    since both of its constraints are inequalities pointing opposite ways.

    Ports with no ``duals`` block are skipped rather than passing vacuously:
    ``pypsa_unit_commitment`` is a MILP, where a dual solution is undefined and
    farkas refuses to invent one.
    """
    expected = port.get('duals')
    if not expected:
        pytest.skip(f'{port["name"]} records no duals (a MILP has none)')

    with fk.solve(port['model'], sources(port['name'])) as solution:
        for constraint, table in expected.items():
            dims = [c for c in table if c != 'value']
            got = solution.dual(constraint).sort(dims)
            want = pl.DataFrame(table).with_columns(pl.col(d).cast(got.schema[d]) for d in dims).sort(dims)

            assert got[dims].equals(want[dims]), f'{port["name"]}.{constraint}: dual is keyed differently'
            assert got['value'].to_list() == pytest.approx(want['value'].to_list(), rel=port['rtol']), (
                f'{port["name"]}.{constraint} disagrees with {port["provenance"]}'
            )
