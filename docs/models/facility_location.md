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

<!-- math:begin -->
<details markdown="1">
<summary>The same model, as math</summary>

#### Sets

| Symbol | Meaning |
|---|---|
| $\mathcal{W}$ | index $w$ --- `warehouse` |
| $\mathcal{C}$ | index $c$ --- `customer` |

#### Parameters

| Symbol | Meaning |
|---|---|
| $\mathit{fixed\_cost}$ | `fixed_cost` over $\mathcal{W}$ |
| $\mathit{serve}^{\mathrm{cost}}$ | `serve_cost` over $\mathcal{W} \times \mathcal{C}$ |

#### Variables

| Symbol | Meaning |
|---|---|
| $\mathit{is\_open}$ | `is_open` over $\mathcal{W}$ |
| $\mathit{serve}$ | `serve` over $\mathcal{W} \times \mathcal{C}$ |

#### Objective

$$\begin{aligned}
\text{total\_cost:} \quad & \min & \sum_{w \in \mathcal{W},\ c \in \mathcal{C}} \left( \mathit{is\_open}_{w} \cdot \mathit{fixed\_cost}_{w} + \mathit{serve}_{w,c} \cdot \mathit{serve}^{\mathrm{cost}}_{w,c} \right)
\end{aligned}$$

#### Subject to

$$\begin{aligned}
\text{every\_customer\_served:} \quad & \sum_{w \in \mathcal{W}} \mathit{serve}_{w,c} & = 1 && \forall\, c \in \mathcal{C} \\
\text{only\_from\_open\_warehouses:} \quad & \mathit{serve}_{w,c} - \mathit{is\_open}_{w} & \le 0 && \forall\, w \in \mathcal{W},\ c \in \mathcal{C}
\end{aligned}$$

#### Variable domains

$$\begin{aligned}
\text{is\_open:} \quad & \mathit{is\_open}_{w} & \in \{0, 1\} && \forall\, w \in \mathcal{W} \\
\text{serve:} \quad & 0 \le \mathit{serve}_{w,c} & \le 1 && \forall\, w \in \mathcal{W},\ c \in \mathcal{C}
\end{aligned}$$

</details>
<!-- math:end -->

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
for a total of **932615.75**.

Ten of them cost 7500 to open. **`w11` costs nothing**: `cap71` gives it a fixed
cost of 0, so it is free and opening it is never a trade-off at all. Worth
noticing, because it is the one warehouse whose presence in the answer says
nothing about the instance being hard — and a reader checking the arithmetic
against "7500 apiece" would come up 7500 short.

## What it exercises

`binary` on its own dimension while a second, larger variable stays continuous;
a two-dimensional parameter against a two-dimensional variable; and a
two-term objective mixing a fixed charge with a flow cost.
