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
