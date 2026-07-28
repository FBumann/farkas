# Examples

**These are fixtures, not samples.** Eleven test modules load them by path, and
`docs/models/` embeds them, so the file you read here is the file CI runs.
Renaming one breaks tests; changing one changes what the docs claim, and a test
will say so.

**Read them explained** in [docs/models/](../docs/models/index.md) — the maths,
what each construct exercises, and for a port a side-by-side against the
reference implementation. This directory is the source; that is the guided tour.

| | |
|---|---|
| `dispatch.yaml` | least-cost generation against a load profile — the smallest complete model |
| `storage.yaml` | dispatch plus a cyclic battery (`roll`) |
| `transport.yaml` | a network: coordinates on a dimension *are* the topology (`group_sum`) |
| `piecewise.yaml` | per-generator convex cost curves (`piecewise:`) |
| `walkthrough.yaml` | the model `walkthrough.py` prints every pipeline stage for |
| `ports/` | eleven models somebody else already solved, checked against an optimum that did not come from us |

`walkthrough.py` runs one model through YAML → schema → AST → plan → frames →
LP text → solution, printing what each stage produces, then two models the
language refuses and why. Its output is committed as `walkthrough.out` and
asserted, so it cannot drift from what the code does:

```bash
python examples/walkthrough.py
```

`ports/` carries three or four files per model — the YAML, the instance, the
recorded objective with its provenance, and a reference implementation
importing no farkas. That last one is absent where the optimum is *published*
and needs nothing of ours to reproduce it: `facility_location` (OR-Library) and
`tsp_mtz` (TSPLIB) cite the literature instead, which is the strongest tier the
corpus has.

Reference scripts are **never run by CI** and carry their dependencies inline;
adding a port is described in
[CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-ported-model).
