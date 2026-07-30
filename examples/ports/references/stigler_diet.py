#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["linopy==0.9.0", "pandas>=2.2", "xarray==2026.7.0", "highspy==1.15.1"]
# # linopy is pinned because it builds the model here; xarray is its data
# # model. pandas is a floor — it only shapes the input tables.
# ///
"""Reference for ``stigler_diet``: the same LP, hand-written in linopy.

    uv run --script examples/ports/references/stigler_diet.py

**Two things verify this port, and they answer different questions.**

The *published* figure is $39.69 a year. Jack Laderman computed it in 1947 at
the National Bureau of Standards as the first serious test of the simplex
method — nine clerks on desk calculators, about 120 man-days. That number is
history, and it is rounded by the arithmetic of the people who produced it.

This script is what the same data gives to a modern solver: $39.6617 a year,
0.08% under Laderman's. The gap is his rounding, not a different model, and the
*composition* of the diet is the stronger corroboration — both arrive at the
same five foods.

Nothing here imports lpspec. The model is a covering LP with no network in it
at all, which is why it is in the corpus: every other verified port is a flow
of something through something.
"""

from __future__ import annotations

import json
from pathlib import Path

import linopy
import pandas as pd

DATA = Path(__file__).resolve().parent.parent / 'data' / 'stigler_diet.json'

#: Laderman (1947), in 1939 dollars. What the port is checked against loosely;
#: `references.json` records this run's exact value for the tight check.
PUBLISHED_ANNUAL = 39.69


def build(data: dict) -> linopy.Model:
    """The port's tables as a linopy model, column for column."""
    foods = pd.Index(data['food']['food'], name='food')
    nutrients = pd.Index(data['nutrient']['nutrient'], name='nutrient')

    minimum = pd.Series(data['daily_minimum']['value'], index=nutrients)
    # the sparse table filled back out: a missing (food, nutrient) pair means
    # that food supplies none of that nutrient
    per_dollar = (
        pd.DataFrame(data['nutrient_per_dollar'])
        .pivot(index='food', columns='nutrient', values='value')
        .reindex(index=foods, columns=nutrients)
        .fillna(0.0)
    )

    m = linopy.Model()
    spend = m.add_variables(lower=0, coords=[foods], name='spend')
    m.add_constraints((spend * per_dollar).sum('food') >= minimum, name='meet_requirement')
    m.add_objective(spend.sum())
    return m


def shadow_prices(m: linopy.Model) -> dict[str, list]:
    """What one more unit of each nutrient per day would cost.

    The most legible dual in the corpus: it is the price of the binding
    nutrient, and the nutrients that are *not* binding come back at zero
    because they arrive free alongside the ones that are.
    """
    dual = m.constraints['meet_requirement'].dual
    return {'nutrient': [str(v) for v in dual.indexes['nutrient']], 'value': [float(v) for v in dual.values]}


def main() -> float:
    m = build(json.loads(DATA.read_text()))
    status, condition = m.solve(solver_name='highs')
    assert status == 'ok', f'{status}: {condition}'
    daily = float(m.objective.value)
    print(f'linopy {linopy.__version__}')
    print(f'objective {daily!r}')
    print(f'annual {daily * 365:.4f} vs published {PUBLISHED_ANNUAL}')
    print(f'duals {json.dumps({"meet_requirement": shadow_prices(m)})}')
    chosen = m.solution['spend'].to_series()
    print((chosen[chosen > 1e-9] * 365).round(2))
    return daily


if __name__ == '__main__':
    main()
