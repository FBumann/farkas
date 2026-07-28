# Ported models

Models somebody else already solved, said again in this language, and checked
against **an optimum that did not come from us**.

Every other test compares farkas against farkas. Even the differential harness
compares two lanes consuming the *same resolved AST*
([hard rule 1](ARCHITECTURE.md#hard-rules)) — which is what makes them an
oracle for each other, and also what they cannot see: a **shared misreading**,
both lanes agreeing on a meaning the modeller did not intend, passes the whole
suite green. This is the net for that class, and the evidence behind
[the ceiling](ARCHITECTURE.md#two-tiers-and-the-ceiling).

| Port | Reference | Optimum |
|---|---|---|
| [Dantzig transport](models/transport_dantzig.md) | published, GAMS model library #1 | 153.675 |
| [PyPSA LOPF rung 1](models/pypsa_transport.md) | PyPSA 1.2.4, its own linopy 0.9.0 | 22000.0 |
| [PyPSA LOPF rung 2](models/pypsa_ramp.md) | PyPSA 1.2.4, its own linopy 0.9.0 | 18200.0 |
| [PyPSA LOPF rung 3](models/pypsa_storage.md) | PyPSA 1.2.4, its own linopy 0.9.0 | 15253.178322993519 |
| [PyPSA LOPF rung 4](models/pypsa_cyclic_storage.md) | PyPSA 1.2.4, its own linopy 0.9.0 | 17228.77962151063 |
| [PyPSA LOPF rung 5](models/pypsa_kvl.md) | PyPSA 1.2.4, its own linopy 0.9.0 | 17000.0 |
| [PyPSA unit commitment](models/pypsa_unit_commitment.md) | PyPSA 1.2.4, its own linopy 0.9.0 | 24900.0 |
| [Dantzig, economies of scale](models/transport_pwl.md) | linopy 0.9.0's own piecewise formulation | 8.786852757777865 |

Adding one is four files and five rules:
[CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-ported-model).

## The ladder

Reproducing a full PyPSA objective means reproducing marginal *and* capital
cost, ramp limits, storage cycling and KVL at once, and a mismatch then
implicates five features instead of one. So each network is a ladder, one
feature per rung, each switched off in PyPSA and reproduced here:
**1 transport model** ✔ · **2 ramp limits** ✔ · **3 storage with state of
charge** ✔ · **4 cyclic boundary condition** ✔ · **5 KVL** ✔ — the ladder is
complete. [Unit
commitment](models/pypsa_unit_commitment.md) sits beside the ladder rather than
on it — one bus, no network, because the feature under test is integrality.

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

Feeds [docs/ROADMAP.md](ROADMAP.md), with the verdict
[CLAUDE.md](../CLAUDE.md) asks for: macro, primitive, or escape.

| Port | What could not be said | Worked around by | Verdict |
|---|---|---|---|
| PyPSA rung 1 | a bound of `-rating` — PyPSA's `p_min_pu = -1` | shipping `neg_rating` as data | **primitive**: bounds as expressions, [#31](https://github.com/FBumann/farkas/issues/31). A second model asking for it |
| PyPSA unit commitment | `min_up_time` — a unit that starts must stay up for *T* snapshots | left at 0, so the constraint is not written | **split verdict**, below |

`min_up_time` is the more interesting row, because the answer depends on
something the language cares about. The constraint is
`sum(start_up over the last T snapshots) <= status`. For a *single T fixed in
the file* that is `start_up + shift(start_up, 1) + …` — a **macro**, free. For
PyPSA's actual signature, where `T` is a column and each generator may have its
own, the number of terms is read from data, and
[data-dependent structure inside an expression](ARCHITECTURE.md#two-tiers-and-the-ceiling)
is refused by design rather than unimplemented. The two halves of that answer
are worth keeping apart: one is a macro nobody has written, the other is the
ceiling doing its job.

Two rows from eight ports — a rate worth watching once the corpus has hit the
ceiling a few more times.

---

Each port has its own page in [the gallery](models/index.md) — the maths, the
instance, the model as CI runs it, and a side-by-side against the reference
implementation. Adding one is four files and five rules:
[CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-ported-model).
