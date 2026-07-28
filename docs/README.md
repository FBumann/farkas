# Docs

| | |
|---|---|
| [**Models**](models/index.md) | every model in the repo — the maths, the YAML, what it exercises, and which two are checked against somebody else's optimum |
| [**SPEC**](SPEC.md) | the language reference: what a YAML file may contain and what it means |
| [**ARCHITECTURE**](ARCHITECTURE.md) | how it fits together, the hard rules, the expressive ceiling, the module map |
| [**ROADMAP**](ROADMAP.md) | what we build toward, and what we have decided never to build |
| [**Benchmarks**](benchmarks.md) | measured cost against the eager lane, and how to reproduce it |
| [benchmarks-scaling.html](benchmarks-scaling.html) | the same run, plotted — open it locally; GitHub will not render it |
| [ports.md](ports.md) | how the verified corpus is built: the ladder it climbs, and the ledger of what a port could *not* say |

Two things that are **not** here, on purpose:

- [**CONTRIBUTING.md**](../CONTRIBUTING.md) is at the repo root, where GitHub
  surfaces it when a PR is opened. Contributors are on GitHub; procedure does
  not need a docs page.
- [**RELEASING.md**](../RELEASING.md), for the same reason.

These files render on GitHub as they are. Nothing here is generated from
anything else — except the construct matrix in
[models/index.md](models/index.md), which is read off each model's resolved
plan by `tools/constructs.py`, and the numbers in `benchmarks.md` and the chart
page, which come from `bench/results/` via `bench.report` and `bench.plot`.
