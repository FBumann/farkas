# Organising the docs — proposal

Draft for review. Not a repo file: it describes work, and once the work lands
the repo's own docs are the record.

## The problem

The docs are organised by **topic**, and they should be organised by
**audience**. `docs/ports.md` is the clearest case — its second paragraph is:

> Even the differential harness compares two lanes consuming the *same resolved
> AST* (hard rule 1) — which is what makes them an oracle for each other…

That is epistemology aimed at a contributor. A user arriving there wants three
things, in order: *can it say my model?* · *is it readable?* · *does it get the
right answer?* All three are currently buried under the argument for why the
corpus exists.

Secondary problems: `docs/` is six unrelated artifacts with no index and no
published site; the four `examples/*.yaml` models have no documentation at all;
and there is no `CONTRIBUTING.md`, so setup, the test loop and "how do I add a
port" live in `CLAUDE.md` or nowhere.

## The principle

**Site is for users. Repo is for contributors. Rationale stays; procedure
moves.**

| Content | Home |
|---|---|
| Model, math, side-by-side, verified badge, limitations | **site** |
| Language reference, benchmarks, roadmap | **site** (sourced from root files) |
| Why the structure is what it is; the ceiling; hard rules | `ARCHITECTURE.md` — unchanged |
| Extension checklists (add a primitive) | `ARCHITECTURE.md` — unchanged, linked from CONTRIBUTING |
| Setup · test loop · what each CI gate means | **`CONTRIBUTING.md`** (new) |
| How to add a port: layout, PEP 723, why references never run in CI, `rtol` | **`CONTRIBUTING.md`** |
| Refreshing the benchmarks | **`CONTRIBUTING.md`** |
| Branch / PR conventions | **`CONTRIBUTING.md`** → links `RELEASING.md` |

The extension checklists stay in `ARCHITECTURE.md` deliberately: they sit
directly under the admissibility test that decides whether a primitive is
allowed at all, and splitting them separates *may I?* from *how?*.

`CONTRIBUTING.md` does **not** go in the site nav. Contributors are on GitHub,
which surfaces the file when a PR is opened.

## The site

`mkdocs-material` → GitHub Pages. The repo is already markdown-native, so the
existing files are the content.

```
Home              index.md — new; short, from README
Models            the gallery (below)
Language          SPEC.md
Benchmarks        docs/benchmarks.md
Architecture      ARCHITECTURE.md
Roadmap           ROADMAP.md
```

**Root files stay at root.** `SPEC.md`, `ARCHITECTURE.md` and `ROADMAP.md` are
referenced across the repo and tracked by name in `test_doc_examples.py`, and
they must keep rendering on GitHub — contributors read them there. The site
pulls them in at build time via an mkdocs hook rather than moving or
duplicating them.

That hook has one real cost: **link rewriting**. Root docs cross-link as
`](SPEC.md)`, `](docs/benchmarks.md)`, `](tests/differential.py)`. In the site
those must become site paths or GitHub blob URLs depending on the target. One
hook, one rule table, applied to every page — but it is the main piece of
machinery this proposal introduces, and it needs a test.

## Models — a gallery, not "ports"

"Ported models" is our word. A user wants to browse models; external
verification is a **badge on an entry**, not the organising principle. Folding
the two views together also documents the four `examples/*.yaml` for the first
time.

```
Models
  ├── dispatch                      sum · where · bounds
  ├── storage                       roll
  ├── transport                     group_sum
  ├── piecewise                     piecewise:
  ├── Dantzig transport      ✔ GAMS #1 · 153.675
  └── PyPSA LOPF rung 1      ✔ PyPSA 1.2.4 · 22000.0
```

Each entry: the math, the YAML, what it exercises, the result — and for a
verified one, the badge and the reference it matched.

### Proving *readable*

**Side by side with the reference implementation**, which we already have in
`examples/ports/references/`. Two columns settle readability in five seconds;
no prose does it faster.

One honesty check: the PyPSA reference is short *because PyPSA is a domain
package* with `Generator` and `Link` objects. Against that, our YAML looks more
explicit, not shorter. The comparison that reflects a real user's alternative
is **hand-written linopy** — which argues for restoring the
`transport_dantzig.py` reference I dropped for compactness. For docs that was
the wrong trade.

Recommendation: both, in mkdocs-material content tabs.

### Proving *capable*

A **generated** construct matrix — which primitives each model exercises, read
off the resolved AST, so it cannot drift.

| Model | `sum` | `group_sum` | `roll` | `where` | `piecewise` | MILP |
|---|---|---|---|---|---|---|

It will be honestly thin: today no *externally verified* model exercises
`roll`, `piecewise` or binaries — ladder rungs 2–5 are what fill those in. Show
the gap. A matrix with holes and a stated plan reads as confident; one that
quietly omits the failing columns reads as marketing.

## Non-goals

- Moving `SPEC.md` / `ARCHITECTURE.md` / `ROADMAP.md`.
- Publishing `CONTRIBUTING.md`, `CLAUDE.md` or `RELEASING.md` to the site.
- Rewriting the language reference. It gets a nav entry, not an edit.
- Docstring/API reference generation. The public surface is a declared model,
  not a Python API (rule 5) — an autogenerated API page would contradict that.

## Decisions needed

1. **Gallery or ports?** Do the four `examples/*.yaml` join the same section as
   the two verified models? *(recommend: yes)*
2. **Side-by-side against PyPSA, hand-written linopy, or both in tabs?**
   *(recommend: both — and restore the linopy reference for Dantzig)*
3. **Issue / PR templates?** None exist. A port-submission template would carry
   the provenance requirements well, but it is scope creep for this pass.

## Sequencing

Each step is independently useful and reviewable:

1. `CONTRIBUTING.md`, and move the contributor half of `docs/ports.md` into it.
   *No new dependencies — worth doing even if the site never ships.*
2. Gallery restructure + generated construct matrix.
3. mkdocs scaffolding: config, the root-file hook and its test, Pages workflow.
4. Landing page and polish.
