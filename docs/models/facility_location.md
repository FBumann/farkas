# Uncapacitated facility location

Where do you put the warehouses? Open a set of them, assign every customer to one, and trade the fixed cost of opening against the cost of serving from further away.

> **✔ Verified against OR-Library's published optimum** — **932615.750**, matched to `rtol=1e-09`.

OR-Library instance `cap71`: 16 candidate warehouses, 50 customers. The optimum
is [published by Beasley](http://people.brunel.ac.uk/~mastjjb/jeb/orlib/uncapinfo.html)
in the file `uncapopt`, alongside the instance itself.

**No reference script.** This is the corpus's strongest provenance tier: the
number comes from the literature, and there is nothing of ours in the loop that
produced it. [Dantzig transport](transport_dantzig.md) is the other one.

It also brings a structure nothing else in the corpus has: **fixed charge**.
Every other verified model prices what flows; this one prices a *decision* —
opening a warehouse costs money whether or not it ends up busy.

## The model

```yaml
# Uncapacitated facility location, OR-Library instance `cap71`: 16 possible
# warehouses, 50 customers. Open a set of warehouses and assign every customer
# to one, trading fixed opening costs against the cost of serving from further
# away. Optimum 932615.750, published by OR-Library. See docs/ports.md.

dimensions:
  warehouse:
    dtype: str
  customer:
    dtype: str

parameters:
  fixed_cost:
    dims: [warehouse]
  # what it costs to serve all of this customer's demand from this warehouse
  serve_cost:
    dims: [warehouse, customer]

variables:
  # is this warehouse open? The only integrality in the model — `serve` comes
  # out integral on its own, which is the point of writing the linking
  # constraint per (warehouse, customer) rather than aggregated.
  is_open:
    foreach: [warehouse]
    binary: true
  serve:
    foreach: [warehouse, customer]
    bounds:
      lower: 0
      upper: 1

constraints:
  every_customer_served:
    foreach: [customer]
    equations:
      - expression: sum(serve, over=warehouse) == 1

  # A closed warehouse serves nobody. Written per pair — the "strong"
  # formulation — because summing it over customers instead would give a valid
  # but much weaker relaxation, and the LP bound is what makes this instance
  # solve at all.
  only_from_open_warehouses:
    foreach: [warehouse, customer]
    equations:
      - expression: serve - is_open <= 0

objectives:
  total_cost:
    sense: minimize
    equations:
      - expression: is_open * fixed_cost + serve * serve_cost
```

**`serve` is not declared binary, and that is the interesting part.** Only
`is_open` carries integrality. `serve` is free to take fractional values and
comes out integral anyway, because the linking constraint is written **per
(warehouse, customer) pair**.

The aggregated alternative — `sum(serve, over=customer) <= 50 * is_open`, one
row per warehouse instead of 800 — is equally *valid* and much *weaker*: its LP
relaxation lets a warehouse open a fiftieth of the way and serve one customer
for a fiftieth of its fixed cost. The strong formulation is what makes the LP
bound tight enough for the instance to solve immediately.

That choice is not something the language makes for you, and it is invisible in
the objective. It is a modelling decision that the corpus happens to pin: get
it wrong and the answer is still 932615.750, just much slower to reach.

## What it finds

Eleven of the sixteen warehouses open — `w01`–`w04`, `w06`–`w09`, `w11`–`w13` —
for a total of **932615.75** against fixed costs of 7500 apiece.

## What it exercises

`binary` on its own dimension while a second, larger variable stays continuous;
a two-dimensional parameter against a two-dimensional variable; and a
two-term objective mixing a fixed charge with a flow cost.
