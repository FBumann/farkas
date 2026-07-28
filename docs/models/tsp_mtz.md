# Travelling salesman — MTZ

Visit every city once and come home, as cheaply as possible. The most famous problem in combinatorial optimisation, and the one most often assumed to be out of reach here.

> **✔ Verified against TSPLIB's published optimum** — **2085**, matched to `rtol=1e-09`. Instance `gr17` (Groetschel, 17 cities, explicit distance matrix).

**It is not out of reach.** This page exists because "we can't do TSP" was
plausible enough to be worth testing rather than asserting, and it turned out
to be wrong.

## What genuinely is refused, and why

TSP's textbook formulation — Dantzig–Fulkerson–Johnson — forbids subtours with
one constraint per subset of cities, or generates them lazily as they are
violated. **Neither is sayable here, and neither should be:**

| | Why it is out |
|---|---|
| DFJ, all subsets | the *number of constraints depends on the data*, and the plan must know every component's extent before data is read ([the ceiling](../ARCHITECTURE.md#two-tiers-and-the-ceiling)) |
| DFJ, lazy cuts | solve, find violations, add rows, re-solve — that is an **algorithm**, not a model. Nothing declarative describes it |

Those refusals are the same rule that makes a plan knowable before any data is
touched, which everything else in the engine rests on. Giving them up to gain
TSP would trade the property for the example.

## What that leaves

Miller–Tucker–Zemlin, the polynomial alternative: give each city a position in
the tour and require that an arc `i → j` puts `j` later than `i`. O(n²) rows,
every one of them known before the data is read — static, relational, degree 1.
Inside the language, and it always was.

## The model

```yaml
# The travelling salesman problem, MTZ formulation. TSPLIB instance `gr17`:
# 17 cities, explicit distance matrix, published optimum 2085.
# See docs/ports.md — this is the port that tests where the ceiling actually is.
#
# TSP's usual formulation (Dantzig-Fulkerson-Johnson) eliminates subtours with
# one constraint per subset of cities, or by generating them lazily as they are
# violated. Neither is sayable here and neither should be: the first has a
# constraint count that depends on the data, the second is a solve loop rather
# than a model. Miller-Tucker-Zemlin is the polynomial alternative — O(n^2)
# rows, all known before any data is read — and it is inside the language.

dimensions:
  # The tour position variable `u` lives on `city`; the arc variable lives on
  # an ordered pair. `as_from` / `as_to` are the identity map from a city onto
  # each end of a pair, which is what lets one variable appear at two different
  # coordinates in the same row.
  city:
    dtype: str
    coords: {as_from: from_city, as_to: to_city}
  from_city:
    dtype: str
  to_city:
    dtype: str

parameters:
  # No row on the diagonal: a city has no distance to itself, so no arc
  # variable exists there and every row mentioning one drops out.
  distance:
    dims: [from_city, to_city]
  n:
    dims: []

variables:
  travel:
    foreach: [from_city, to_city]
    where: distance
    binary: true
  # Position of each city in the tour. Continuous — MTZ needs only that the
  # positions be orderable, not that they be whole numbers.
  u:
    foreach: [city]
    bounds:
      lower: 1
      upper: 17

constraints:
  leave_each_city_once:
    foreach: [from_city]
    equations:
      - expression: sum(travel, over=to_city) == 1

  enter_each_city_once:
    foreach: [to_city]
    equations:
      - expression: sum(travel, over=from_city) == 1

  # Miller-Tucker-Zemlin: if the tour goes i -> j then j is later than i, and
  # the big-M is slack enough to say nothing when it does not. Written for
  # every ordered pair except those touching the depot, which anchors the
  # numbering.
  #
  # `group_sum(u, over=city, by=as_from)` is a *relabel*, not a reduction: the
  # coordinate map is one-to-one, so it moves `u` from the `city` axis onto the
  # `from_city` axis without adding anything up. Doing it twice with different
  # coordinates is how the same variable appears at both ends of one row.
  ordering:
    foreach: [from_city, to_city]
    where: "from_city != c01 AND to_city != c01"
    equations:
      - expression: group_sum(u, over=city, by=as_from) - group_sum(u, over=city, by=as_to) + n * travel <= n - 1

objectives:
  tour_length:
    sense: minimize
    equations:
      - expression: travel * distance
```

**One shape here is worth the whole page.** MTZ needs `u` — the tour position —
at *both ends of the same row*: `u_i − u_j`. A variable indexed by one
dimension appearing twice under two different roles is exactly the kind of
self-join that looks like it should need a primitive.

It does not. Declare the identity map from `city` onto each end of the pair:

```yaml
city:
  coords: {as_from: from_city, as_to: to_city}
```

and `group_sum(u, over=city, by=as_from)` becomes a **relabel** rather than a
reduction — the map is one-to-one, so nothing is added up; `u` simply moves
from the `city` axis onto the `from_city` axis. Doing it twice with different
coordinates puts the same variable at both ends of one row.

That is `group_sum` doing a job it was not designed for and handling it because
[topology is data](pypsa_transport.md): a coordinate map is a join, and a join
does not care whether it is many-to-one or one-to-one.

**The diagonal takes care of itself.** `distance` has no row where a city meets
itself, `travel`'s `where` is that parameter, and absence spreads — so every
row mentioning a self-arc simply is not built. No `i ≠ j` guard is written
anywhere, because [dimension-to-dimension comparison is not in the
language](../SPEC.md#6-where-strings) and here it is not needed.

## What it finds

A single tour of all 17 cities, closing at the start:

```
c01 → c16 → c12 → c09 → c05 → c02 → c10 → c11 → c03
    → c15 → c14 → c17 → c06 → c08 → c07 → c13 → c04 → c01
```

Length **2085**, TSPLIB's published optimum. One tour, not several — which is
the whole thing MTZ is there to guarantee, and worth checking on the primal
rather than trusting the objective, since a subtour-ridden solution would be
*cheaper*, not more expensive.

It solves in about two and a half seconds. MTZ's LP relaxation is famously
weak — that is the price of the formulation being small, and it is why nobody
solves large instances this way.

## What it exercises

`group_sum` as a relabel through a one-to-one coordinate map, a `where`
comparing a dimension against a string coordinate, sparsity standing in for an
`i ≠ j` guard, and `binary` over a two-dimensional index.

No new construct. The honest summary is that the ceiling refuses an
*algorithm*, not a *problem* — and the corpus is a better place to find that
out than an argument.
