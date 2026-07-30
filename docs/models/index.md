# Models

Every model in the repo, what it says, and what it exercises. Three questions,
in the order you probably have them: **can it say my model?** · **is it
readable?** · **does it get the right answer?**

| | |
|---|---|
| [dispatch](dispatch.md) | least-cost generation against a load profile |
| [storage](storage.md) | dispatch plus a cyclic battery |
| [transport](transport.md) | a network — generators on buses, lines between them |
| [piecewise](piecewise.md) | per-generator convex cost curves |
| [walkthrough](walkthrough.md) | the model behind every pipeline stage, printed |
| [Dantzig transport](transport_dantzig.md) ✔ | GAMS model library #1 |
| [PyPSA LOPF rung 1](pypsa_transport.md) ✔ | PyPSA's own transport model |
| [PyPSA LOPF rung 2](pypsa_ramp.md) ✔ | rung 1 plus generator ramp limits |
| [PyPSA LOPF rung 3](pypsa_storage.md) ✔ | rung 2 plus storage carrying energy in time |
| [PyPSA LOPF rung 4](pypsa_cyclic_storage.md) ✔ | rung 3 with the horizon closed on itself |
| [PyPSA LOPF rung 5](pypsa_kvl.md) ✔ | passive AC lines under Kirchhoff's voltage law |
| [PyPSA unit commitment](pypsa_unit_commitment.md) ✔ | which units are *on* — the corpus's MILP |
| [Dantzig, economies of scale](transport_pwl.md) ✔ | GAMS `trnspwl` — piecewise `sqrt` shipping cost |
| [Stigler's diet](stigler_diet.md) ✔ | the cheapest year of food — where LP started |
| [Facility location](facility_location.md) ✔ | OR-Library `cap71` — which warehouses to open |
| [Travelling salesman](tsp_mtz.md) ✔ | TSPLIB `gr17`, MTZ — yes, it fits |

Both tables below are **generated** — the constructs matrix off each model's
resolved plan, the reference table off `examples/ports/references.json`, which
is the same file the tests assert against. Regenerate with
`uv run python -m tools.constructs`; a test fails if either is stale.

Every page also carries the model **as math**, typeset from the same file the
engine builds (`uv run python -m tools.gallery_math`, likewise gated). Where a
model has a symbol table in `examples/symbols/`, the block uses the notation
that model's prose already does.

## Can it say my model?

Read off the resolved plan of each model rather than its text, so it cannot
drift from what the engine builds.

<!-- constructs:begin -->
| model | verified | `sum` | `group_sum` | `roll / shift` | `where` | `bounds` | `piecewise` | MILP |
|---|---|---|---|---|---|---|---|---|
| [dispatch](dispatch.md) | · | **✓** | · | · | **✓** | **✓** | · | · |
| [piecewise](piecewise.md) | · | **✓** | · | · | · | **✓** | **✓** | · |
| [storage](storage.md) | · | **✓** | · | **✓** | · | **✓** | · | · |
| [transport](transport.md) | · | · | **✓** | · | · | **✓** | · | · |
| [walkthrough](walkthrough.md) | · | **✓** | · | · | **✓** | **✓** | · | · |
| [facility_location](facility_location.md) | **✔** 932616 | **✓** | · | · | · | **✓** | · | **✓** |
| [pypsa_cyclic_storage](pypsa_cyclic_storage.md) | **✔** 17228.8 | · | **✓** | **✓** | **✓** | **✓** | · | · |
| [pypsa_kvl](pypsa_kvl.md) | **✔** 17000 | **✓** | **✓** | · | · | **✓** | · | · |
| [pypsa_ramp](pypsa_ramp.md) | **✔** 18200 | · | **✓** | **✓** | **✓** | **✓** | · | · |
| [pypsa_storage](pypsa_storage.md) | **✔** 15253.2 | · | **✓** | **✓** | **✓** | **✓** | · | · |
| [pypsa_transport](pypsa_transport.md) | **✔** 22000 | · | **✓** | · | · | **✓** | · | · |
| [pypsa_unit_commitment](pypsa_unit_commitment.md) | **✔** 24900 | **✓** | · | **✓** | **✓** | **✓** | · | **✓** |
| [stigler_diet](stigler_diet.md) | **✔** 0.108662 | **✓** | · | · | · | **✓** | · | · |
| [transport_dantzig](transport_dantzig.md) | **✔** 153.675 | **✓** | · | · | · | **✓** | · | · |
| [transport_pwl](transport_pwl.md) | **✔** 8.78685 | **✓** | · | **✓** | · | **✓** | **✓** | **✓** |
| [tsp_mtz](tsp_mtz.md) | **✔** 2085 | **✓** | **✓** | · | **✓** | **✓** | · | **✓** |
<!-- constructs:end -->

**No holes left.** Every construct in the table has at least one model behind
it whose optimum came from somebody else. The column was worth keeping
precisely because it named each gap out loud before it was filled: `roll /
shift` went first ([ramp limits](pypsa_ramp.md)), then integrality
([unit commitment](pypsa_unit_commitment.md)), and `piecewise` last
([economies of scale](transport_pwl.md)).

That is a floor, not a ceiling. A tick means *one* verified model exercises the
construct — not that every shape of it is covered, and not that the constructs
are exercised in combination. The table's job from here is to stay honest as
the language grows, which is why it is generated rather than maintained by
hand.

## Does it get the right answer?

**✔ means the optimum did not come from us.** Every model on this page is run
by the test suite, so "there is a test" distinguishes nothing. What the badge
marks is narrower, and it is the only check that can catch a *shared
misreading* — both lanes of the implementation agreeing on a meaning the
modeller did not intend, which passes every lpspec-against-lpspec test green.

Even the differential harness compares two lanes consuming the *same resolved
AST* ([hard rule 1](../ARCHITECTURE.md#hard-rules)), which is what makes them
an oracle for each other and also what they cannot see. This is the net for
that class, and the evidence behind
[the ceiling](../design/ceiling.md#two-tiers-and-the-ceiling).

<!-- references:begin -->
| port | optimum | `rtol` | duals | reference |
|---|---|---|---|---|
| [facility_location](facility_location.md) | 932615.75 | 1e-09 | · | published by OR-Library (Beasley) for instance cap71 of the uncapacitated warehouse location set, in the file uncapopt: http://people.brunel.ac.uk/~mastjjb/jeb/orlib/uncapinfo.html |
| [pypsa_cyclic_storage](pypsa_cyclic_storage.md) | 17228.77962151063 | 1e-09 | **✔** | pypsa 1.2.4 (its own linopy 0.9.0), via examples/ports/references/pypsa_cyclic_storage.py |
| [pypsa_kvl](pypsa_kvl.md) | 17000.0 | 1e-09 | **✔** | pypsa 1.2.4 (its own linopy 0.9.0), via examples/ports/references/pypsa_kvl.py |
| [pypsa_ramp](pypsa_ramp.md) | 18200.0 | 1e-09 | **✔** | pypsa 1.2.4 (its own linopy 0.9.0), via examples/ports/references/pypsa_ramp.py |
| [pypsa_storage](pypsa_storage.md) | 15253.178322993519 | 1e-09 | **✔** | pypsa 1.2.4 (its own linopy 0.9.0), via examples/ports/references/pypsa_storage.py |
| [pypsa_transport](pypsa_transport.md) | 22000.0 | 1e-09 | **✔** | pypsa 1.2.4 (its own linopy 0.9.0), via examples/ports/references/pypsa_transport.py |
| [pypsa_unit_commitment](pypsa_unit_commitment.md) | 24900.0 | 1e-09 | · | pypsa 1.2.4 (its own linopy 0.9.0), via examples/ports/references/pypsa_unit_commitment.py |
| [stigler_diet](stigler_diet.md) | 0.10866227820675685 | 1e-09 | **✔** | linopy 0.9.0, via examples/ports/references/stigler_diet.py — dollars per day; x365 = $39.6617/year[^stigler_diet] |
| [transport_dantzig](transport_dantzig.md) | 153.675 | 1e-09 | **✔** | published with GAMS model library #1 (trnsport), after Dantzig, Linear Programming and Extensions (1963) ch. 3.3[^transport_dantzig] |
| [transport_pwl](transport_pwl.md) | 8.786852757777865 | 1e-09 | · | linopy 0.9.0's own add_piecewise_formulation, via examples/ports/references/transport_pwl.py; the model is GAMS model library trnspwl (Dantzig transport with economies of scale), which publishes the formulation and its discretisation but no optimal objective |
| [tsp_mtz](tsp_mtz.md) | 2085.0 | 1e-09 | · | published by TSPLIB for instance gr17 (Groetschel, 17 cities, EXPLICIT lower-diagonal distance matrix); optimum 2085 as listed in the TSPLIB solutions file |

[^stigler_diet]: Laderman (1947) at the National Bureau of Standards published $39.69/year for this data, the first serious test of the simplex method. This LP's exact optimum is 0.08% under it — his rounding, not a different model — and both select the same five foods: wheat flour, liver, cabbage, spinach, navy beans.

[^transport_dantzig]: examples/ports/references/transport_dantzig.py — the same LP hand-written in linopy 0.9.0, which reaches 153.675 independently. Secondary: the published figure is what verifies the port.
<!-- references:end -->

**The objective is not the only thing checked.** A port with a `duals` tick
also records the reference's **shadow prices** and is asserted against them —
for the PyPSA models that is `buses_t.marginal_price`, the nodal price, which
is the output this audience reads most often after the cost.

That matters because an objective is one number and hides a great deal. A dual
vector is where two implementations most reliably disagree quietly: which side
of a constraint the price belongs to, and what sign an inequality carries.
[Dantzig transport](transport_dantzig.md) is in that set specifically because
both of its constraints are inequalities pointing opposite ways. A MILP has no
dual solution, and lpspec refuses to invent one — which is what the `·` rows
are.

Adding a port is four files and five rules:
[CONTRIBUTING.md](https://github.com/FBumann/lpspec/blob/main/CONTRIBUTING.md#adding-a-ported-model).

## The ladder

Reproducing a full PyPSA objective means reproducing marginal *and* capital
cost, ramp limits, storage cycling and KVL at once, and a mismatch then
implicates five features instead of one. So each network is a ladder, one
feature per rung, each switched off in PyPSA and reproduced here:
**1 transport model** ✔ · **2 ramp limits** ✔ · **3 storage with state of
charge** ✔ · **4 cyclic boundary condition** ✔ · **5 KVL** ✔ — the ladder is
complete. [Unit commitment](pypsa_unit_commitment.md) sits beside the ladder
rather than on it — one bus, no network, because the feature under test is
integrality.

A rung that matches is a row in the table above; one that **cannot be said** is
a row in the ledger. Both are evidence, so no rung is wasted.

Rung 2 needed the instance widened before it meant anything. Rung 1's links run
saturated, which fixes every generator's output exactly, so a ramp limit on that
network can only make it infeasible — never change the answer. A rung that
cannot bind is not evidence that it works.

**The ladder finished without a new primitive.** Rung 5 is Kirchhoff's voltage
law, and it needed nothing added to the language: a cycle basis is a sparse
`(cycle, line)` incidence *parameter*, and the constraint is one
`sum(f * cycle_incidence, over=line) == 0`. A line can belong to several
cycles, so the incidence cannot be a declared coordinate — that is the shape
finding, and it is the same "topology is data" claim the corpus started with.
Computing the basis is a graph algorithm and stays in data preparation, where
the ceiling puts it.

**Rung 4 made the model smaller**, which is the ladder paying off in the
direction nobody plans for. Closing the horizon deletes rung 3's boundary
equation outright: `roll` is cyclic already, so the wrap onto the last snapshot
is what it does unguarded, and the *acyclic* case is the one needing an extra
clause. Two rungs written a day apart, differing by one deleted `where`, is a
sharper statement about the language than either alone.

## Ledger — what a port could not say

Feeds [ROADMAP](../ROADMAP.md), with the verdict
[CLAUDE.md](https://github.com/FBumann/lpspec/blob/main/CLAUDE.md) asks for:
macro, primitive, or escape.

| Port | What could not be said | Worked around by | Verdict |
|---|---|---|---|
| PyPSA rung 1 | a bound of `-rating` — PyPSA's `p_min_pu = -1` | shipping `neg_rating` as data | **primitive**: bounds as expressions, [#31](https://github.com/FBumann/lpspec/issues/31). A second model asking for it |
| PyPSA unit commitment | `min_up_time` — a unit that starts must stay up for *T* snapshots | left at 0, so the constraint is not written | **split verdict**, below |
| Travelling salesman | subtour cuts **generated lazily** inside branch-and-cut, which is how every serious TSP code works | [MTZ](tsp_mtz.md), O(n²) and static | **refused, and correctly**: a solve loop is an algorithm, not a model |

`min_up_time` is the more interesting row, because the answer depends on
something the language cares about. The constraint is
`sum(start_up over the last T snapshots) <= status`. For a *single T fixed in
the file* that is `start_up + shift(start_up, 1, fill=0) + …` — a **macro**,
free, and `fill=0` because a window reaching before the horizon is short a term
rather than undefined ([law 8](../SPEC.md#0-the-laws)). For PyPSA's actual
signature, where `T` is a column and each generator may have its own, the
number of terms is read from data, and
[data-dependent structure inside an expression](../design/ceiling.md#two-tiers-and-the-ceiling)
is refused by design rather than unimplemented. The two halves of that answer
are worth keeping apart: one is a macro nobody has written, the other is the
ceiling doing its job.

Three rows from eleven ports — a rate worth watching once the corpus has hit
the ceiling a few more times.

**The TSP row is the one to read**, and it is narrower than it first looked.
Writing DFJ's subtour rows out in full *is* sayable — the subsets go in as data
exactly the way [KVL's cycle basis](pypsa_kvl.md) does, and an 8-city instance
with all 246 subsets solves to a correct tour. There are 2ⁿ of them, so it
stops being practical around twenty cities, but that is a data-size wall rather
than a ceiling.

What is genuinely outside is *lazy* generation: solve, find the violated
subsets, add rows, re-solve. That is an algorithm, and this language describes
models. Since lazy generation is what every serious TSP code actually does,
"lpspec can express TSP" and "lpspec is a good way to solve a large TSP" are
different sentences and only the first is true.

An earlier draft of this ledger said DFJ was refused for having a
data-dependent row count. That was wrong — the cycle basis has one too — and
the corpus is where it got caught.
