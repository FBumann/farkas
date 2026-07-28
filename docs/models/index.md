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
| [PyPSA unit commitment](pypsa_unit_commitment.md) ✔ | which units are *on* — the corpus's MILP |

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
| [PyPSA LOPF rung 2](pypsa_ramp.md) | PyPSA 1.2.4, running its own linopy | 18200.0 |
| [PyPSA LOPF rung 3](pypsa_storage.md) | PyPSA 1.2.4, running its own linopy | 15253.178322993519 |
| [PyPSA LOPF rung 4](pypsa_cyclic_storage.md) | PyPSA 1.2.4, running its own linopy | 17228.77962151063 |
| [PyPSA unit commitment](pypsa_unit_commitment.md) | PyPSA 1.2.4, running its own linopy | 24900.0 |

**The objective is not the only thing checked.** Every port that has a dual
solution also records the reference's **shadow prices** and is asserted against
them — for the PyPSA models that is `buses_t.marginal_price`, the nodal price,
which is the output this audience reads most often after the cost.

That matters because an objective is one number and hides a great deal. A dual
vector is where two implementations most reliably disagree quietly: which side
of a constraint the price belongs to, and what sign an inequality's carries.
[Dantzig transport](transport_dantzig.md) is in that set specifically because
both of its constraints are inequalities pointing opposite ways.
[Unit commitment](pypsa_unit_commitment.md) is not: a MILP has no dual
solution, and farkas refuses to invent one.

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
| [pypsa_cyclic_storage](pypsa_cyclic_storage.md) | **✔** 17228.8 | · | **✓** | **✓** | **✓** | **✓** | · | · |
| [pypsa_ramp](pypsa_ramp.md) | **✔** 18200 | · | **✓** | **✓** | **✓** | **✓** | · | · |
| [pypsa_storage](pypsa_storage.md) | **✔** 15253.2 | · | **✓** | **✓** | **✓** | **✓** | · | · |
| [pypsa_transport](pypsa_transport.md) | **✔** 22000 | · | **✓** | · | · | **✓** | · | · |
| [pypsa_unit_commitment](pypsa_unit_commitment.md) | **✔** 24900 | **✓** | · | **✓** | **✓** | **✓** | · | **✓** |
| [transport_dantzig](transport_dantzig.md) | **✔** 153.675 | **✓** | · | · | · | **✓** | · | · |
<!-- constructs:end -->

**One hole left, and it is the point of showing the table.** `piecewise` is the
only construct with no externally verified model behind it. `roll / shift` was
closed by [ramp limits](pypsa_ramp.md) and integrality by
[unit commitment](pypsa_unit_commitment.md); both were named here as gaps
before they were filled, which is what the column is for.

Every column is a construct that works and is tested. What the table shows is
which ones have been checked against **somebody else's** answer — and for
`piecewise`, the honest answer is still *not yet*.
