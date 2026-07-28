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

## Does it get the right answer?

**✔ means the optimum did not come from us.** Every model on this page is run
by the test suite, so "there is a test" distinguishes nothing. What the badge
marks is narrower, and it is the only check that can catch a *shared
misreading* — both lanes of the implementation agreeing on a meaning the
modeller did not intend, which passes every farkas-against-farkas test green.

| model | reference | optimum |
|---|---|---|
| [Dantzig transport](transport_dantzig.md) | published with GAMS model library #1 | 153.675 |
| [PyPSA LOPF rung 1](pypsa_transport.md) | PyPSA 1.2.4, running its own linopy | 22000.0 |

How a port is put together, the ladder it climbs, and the ledger of what a port
could *not* say: [ports.md](../ports.md).

## Can it say my model?

Read off the resolved plan of each model rather than its text, so it cannot
drift from what the engine builds. Regenerate with
`uv run python -m tools.constructs`; a test fails if it is stale.

<!-- constructs:begin -->
| model | verified | `sum` | `group_sum` | `roll / shift` | `where` | `bounds` | `piecewise` | MILP |
|---|---|---|---|---|---|---|---|---|
| [dispatch](dispatch.md) | · | **✓** | · | · | **✓** | **✓** | · | · |
| [piecewise](piecewise.md) | · | **✓** | · | · | · | **✓** | **✓** | · |
| [storage](storage.md) | · | **✓** | · | **✓** | · | **✓** | · | · |
| [transport](transport.md) | · | · | **✓** | · | · | **✓** | · | · |
| [walkthrough](walkthrough.md) | · | **✓** | · | · | **✓** | **✓** | · | · |
| [pypsa_transport](pypsa_transport.md) | **✔** 22000 | · | **✓** | · | · | **✓** | · | · |
| [transport_dantzig](transport_dantzig.md) | **✔** 153.675 | **✓** | · | · | · | **✓** | · | · |
<!-- constructs:end -->

**The holes are the point.** No externally verified model yet exercises
`roll / shift`, `piecewise` or an integrality constraint — both ports in the
corpus are pure continuous LPs built from `sum` or `group_sum` and bounds.
Rungs 2–5 of the PyPSA ladder are what fill in the storage constructs; a MILP
port has no candidate yet.

Every column is a construct that works and is tested. What the table shows is
which ones have been checked against **somebody else's** answer, and for three
of them the honest answer is *not yet*.
