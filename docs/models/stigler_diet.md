# Stigler's diet problem

The cheapest way to eat for a year and stay alive. 77 foods, 9 nutrients, 1939 prices.

> **✔ Verified against linopy 0.9.0** — objective **0.10866227820675685** dollars/day, matched to `rtol=1e-09`.
> **Corroborated by Laderman (1947)**, who published **$39.69/year** for this data.

This is where linear programming started earning its keep. Stigler posed it in
1945 and got $39.93 by trial and error, admitting there was "no direct method"
to do better. In 1947 Jack Laderman at the National Bureau of Standards took it
as the first serious test of Dantzig's new simplex method: nine clerks on desk
calculators, roughly **120 man-days**, for $39.69.

It is in the corpus because every other verified model is a flow of something
through a network. This one has no network at all — it is a **covering LP**,
`min 1ᵀx` subject to `Ax ≥ b`, which is a different shape of problem reaching
the same engine.

## The model

<!-- math:begin -->
<details>
<summary>The same model, as math</summary>

#### Sets

| Symbol | Meaning |
|---|---|
| $\mathcal{F}$ | index $f$ --- `food` |
| $\mathcal{N}$ | index $n$ --- `nutrient` |

#### Parameters

| Symbol | Meaning |
|---|---|
| $\mathit{nutrient}^{\mathrm{per,dollar}}$ | `nutrient_per_dollar` over $\mathcal{F} \times \mathcal{N}$ |
| $\mathit{daily\_minimum}$ | `daily_minimum` over $\mathcal{N}$ |

#### Variables

| Symbol | Meaning |
|---|---|
| $\mathit{spend}$ | `spend` over $\mathcal{F}$ |

#### Objective

$$\begin{aligned}
\text{total\_cost:} \quad & \min & \sum_{f \in \mathcal{F}} \mathit{spend}_{f}
\end{aligned}$$

#### Subject to

$$\begin{aligned}
\text{meet\_requirement:} \quad & \sum_{f \in \mathcal{F}} \mathit{spend}_{f} \cdot \mathit{nutrient}^{\mathrm{per,dollar}}_{f,n} & \ge \mathit{daily\_minimum}_{n} && \forall\, n \in \mathcal{N}
\end{aligned}$$

#### Variable domains

$$\begin{aligned}
\text{spend:} \quad & \mathit{spend}_{f} & \ge 0 && \forall\, f \in \mathcal{F}
\end{aligned}$$

</details>
<!-- math:end -->

```yaml
# Stigler's diet problem (1945): the cheapest set of foods meeting a year's
# nutritional minimums. Laderman solved it in 1947 on desk calculators as the
# first serious test of the simplex method — nine clerks, about 120 man-days,
# for an annual cost of $39.69. See docs/ports.md.
#
# Stigler's table is normalised per dollar spent, so a variable is *money on a
# food per day* rather than a quantity, and the objective is simply the total.

dimensions:
  food:
    dtype: str
  nutrient:
    dtype: str

parameters:
  # How much of each nutrient a dollar of each food buys. Sparse on purpose:
  # a food supplying none of a nutrient has no row, which is how this language
  # spells absence everywhere. 570 of the 693 cells are non-zero.
  nutrient_per_dollar:
    dims: [food, nutrient]
  daily_minimum:
    dims: [nutrient]

variables:
  # dollars per day spent on this food
  spend:
    foreach: [food]
    bounds:
      lower: 0

constraints:
  meet_requirement:
    foreach: [nutrient]
    equations:
      - expression: sum(spend * nutrient_per_dollar, over=food) >= daily_minimum

objectives:
  total_cost:
    sense: minimize
    equations:
      - expression: spend
```

Stigler's table is normalised **per dollar spent**, so a variable is *money on
a food per day* rather than a quantity, and the objective is just the total.
That is his framing, not a convenience: it is what makes the matrix
price-independent.

**The nutrient table is sparse and stays that way.** 570 of the 693
(food, nutrient) cells are non-zero; a food supplying none of a nutrient simply
has no row. Row absence is how this language spells "not present" everywhere
else, and here it means exactly what a reader would assume.

## What it finds

| food | $/year |
|---|---|
| navy beans (dried) | 22.28 |
| wheat flour (enriched) | 10.77 |
| cabbage | 4.09 |
| spinach | 1.83 |
| beef liver | 0.69 |
| **total** | **39.66** |

**Those are the five foods in the historical solution.** The 0.08% gap against
Laderman's $39.69 is his rounding — nine people with desk calculators — not a
different model. Matching the *composition* is the stronger corroboration; two
routes to the same five foods out of seventy-seven is not a coincidence.

## What a nutrient costs

The duals are the most legible in the corpus — each is what one more unit of
that nutrient per day would cost:

| nutrient | shadow price |
|---|---|
| calcium | 0.0317 |
| vitamin B2 | 0.0164 |
| calories | 0.0088 |
| vitamin A | 0.0004 |
| vitamin C | 0.00014 |
| protein · iron · vitamin B1 · niacin | **0** |

Four of the nine requirements cost nothing at the margin: they arrive free
alongside the ones that bind. That is the diet problem's actual lesson, and it
is a dual, not a primal — which is why the corpus checks duals as well as
objectives.

## Side by side

```python
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
```

## What it exercises

A two-dimensional parameter multiplying a one-dimensional variable, reduced
along the shared dimension — the same shape [KVL](pypsa_kvl.md) needs for its
cycle incidence, doing a completely different job. Plus a bare variable as the
whole objective, which is as small as an objective gets.
