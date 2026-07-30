# Docs

This folder is both the published site and what you read on GitHub.
[index.md](index.md) is the site's front door and this page is the folder view;
start at [writing a model](guide.md), then [the rules](SPEC.md#0-the-laws) for
the exact one.

Two link rules make one set of files serve both places, and
`tests/test_docs_site.py` enforces them: **inside `docs/`, link relatively**;
**outside it, write the full GitHub URL** — the relative form resolves in the
repo and 404s on the site, silently. The rest is in *the docs* in
[CONTRIBUTING.md](../CONTRIBUTING.md#the-docs).

**Generated, so do not hand-edit:** the construct matrix and the reference
table in [models/index.md](models/index.md) (`tools/constructs.py`), the *"the
same model, as math"* block on each model page (`tools/gallery_math.py`), and
the tables in [benchmarks.md](benchmarks.md) (`bench.report`, `bench.plot`).
The YAML and Python shown on the model pages and in the guide is asserted
against the files that run, so a page cannot quietly drift from what it
describes.

**What stays hand-written, and what checks it.** A model page opens with a
summary — a sentence and, on six pages, the math stated the way that problem is
usually written. That is allowed to be loose in a way the generated block
beneath it is not: it is read at a glance, and three summaries had drifted far
enough to be wrong before the block existed to check them against. So the
looseness is bounded rather than assumed. `tests/test_typeset.py` requires each
of those six to **either** use only symbols the generator can reach — the
hand-written notation is then an oracle *for* the typesetter, since the point
of the format is that a gallery page could be generated — **or** name why it
deviates. `tsp_mtz` states DFJ, the formulation the language refuses; `storage`
writes `soc_{s-1}` where the model rolls and the generator writes the cyclic
`⊖`. A page in neither list fails, so a new summary cannot quietly opt out.
