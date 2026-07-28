# Ported models

Models somebody else already solved, said again in this language, and checked
against **an optimum that did not come from us**.

Every other test compares farkas against farkas. Even the differential harness
compares two lanes consuming the *same resolved AST*
([hard rule 1](../ARCHITECTURE.md#hard-rules)) — which is what makes them an
oracle for each other, and also what they cannot see: a **shared misreading**,
both lanes agreeing on a meaning the modeller did not intend, passes the whole
suite green. This is the net for that class, and the evidence behind
[the ceiling](../ARCHITECTURE.md#two-tiers-and-the-ceiling).

| Port | Reference | Optimum |
|---|---|---|
| [Dantzig transport](#dantzigs-transportation-problem) | published, GAMS model library #1 | 153.675 |
| [PyPSA LOPF rung 1](#pypsa-lopf--rung-1) | PyPSA 1.2.4, its own linopy 0.9.0 | 22000.0 |

Adding one is four files and five rules:
[CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-ported-model).

## The ladder

Reproducing a full PyPSA objective means reproducing marginal *and* capital
cost, ramp limits, storage cycling and KVL at once, and a mismatch then
implicates five features instead of one. So each network is a ladder, one
feature per rung, each switched off in PyPSA and reproduced here: **1 transport
model** (below) · 2 ramp limits · 3 storage with state of charge · 4 cyclic
boundary condition · 5 KVL.

A rung that matches is a row in the table above; one that **cannot be said** is
a row in the ledger. Both are evidence, so no rung is wasted.

## Ledger — what a port could not say

Feeds [ROADMAP.md](../ROADMAP.md), with the verdict
[CLAUDE.md](../CLAUDE.md) asks for: macro, primitive, or escape.

| Port | What could not be said | Worked around by | Verdict |
|---|---|---|---|
| PyPSA rung 1 | a bound of `-rating` — PyPSA's `p_min_pu = -1` | shipping `neg_rating` as data | **primitive**: bounds as expressions, [#31](https://github.com/FBumann/farkas/issues/31). A second model asking for it |

One row from two ports — a rate worth watching once the corpus has hit the
ceiling a few more times.

---

## The two ports

Each has its own page in the gallery — the maths, the instance, the model as
CI runs it, and a side-by-side against the reference implementation:

- [**Dantzig transport**](models/transport_dantzig.md) — GAMS model library #1,
  optimum 153.675 published with the model
- [**PyPSA LOPF rung 1**](models/pypsa_transport.md) — PyPSA 1.2.4 running its
  own linopy, optimum 22000.0

Adding one is four files and five rules:
[CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-ported-model).
