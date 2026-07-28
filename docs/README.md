# Docs

**New here?** [Writing a model](guide.md) — five ideas, each shown in a model
that runs. Then [the models](models/index.md) to browse, and
[SPEC](SPEC.md) when you need the exact rule.

| | |
|---|---|
| [**Writing a model**](guide.md) | the guide: dimensions, absence, topology, `roll`, the dim algebra — and what the language will *not* do |
| [**Models**](models/index.md) | every model in the repo, what each exercises, and which two are checked against somebody else's optimum |
| [**SPEC**](SPEC.md) | the reference: what a YAML file may contain and what it means |
| [**Benchmarks**](benchmarks.md) | what a build costs against the eager lane, and how to reproduce it |
| [**ARCHITECTURE**](ARCHITECTURE.md) | why it is shaped this way — the hard rules, the expressive ceiling, the module map |
| [**ROADMAP**](ROADMAP.md) | what we build toward, and what we have decided never to build |
| [ports.md](ports.md) | how the verified corpus works: the ladder it climbs, and the ledger of what a port could *not* say |
| [benchmarks-scaling.html](benchmarks-scaling.html) | the benchmark run, plotted — open it locally; GitHub will not render it |

Working on it rather than with it: [CONTRIBUTING.md](../CONTRIBUTING.md) and
[RELEASING.md](../RELEASING.md) are at the repo root, where GitHub surfaces
them when a PR is opened.

**What is generated, and must not be hand-edited:** the construct matrix in
[models/index.md](models/index.md) is read off each model's resolved plan by
`tools/constructs.py`; the tables in [benchmarks.md](benchmarks.md) and the
numbers in the chart page come from `bench/results/` via `bench.report` and
`bench.plot`. Everything else here is written by hand — and the YAML and
Python shown on the model pages and in the guide is asserted against the files
that run, so a page cannot quietly drift from the thing it describes.
