# Contributing

Procedure lives here. **Why** the project is shaped the way it is lives in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), and that split is deliberate: this file
should be readable in one sitting and go stale only when a command changes.

## Setup

```bash
uv sync  # dev group is the default: tools + the linopy oracle
uv run pre-commit install  # once per clone
```

`uv sync` installs the `[linopy]` extra too, because the differential test
suite needs a second lane to compare against. The engine itself never imports
linopy, xarray or pandas — see *the bare install* below.

## The loop

```bash
uv run pytest -q  # ~20 s
uv run ruff check --fix . && uv run ruff format .
uv run pyrefly check
```

Narrower runs while working:

```bash
uv run pytest tests/test_relational.py -q
uv run pytest -k piecewise -q
uv run pytest --lf  # last failures only
```

## What each CI gate means

`main` requires two checks: **`ci`** and **`Conventional commit subject`**.
Everything below is the first one, in the order it runs.

| gate | what a failure means |
|---|---|
| `ruff check .` | a lint rule fired. `--fix` handles most; if the finding is wrong, silence the one line with a `# noqa: RULE` and say why. |
| `ruff format --check .` | formatting drifted. Run `ruff format .`. |
| `pyrefly check` | a type is wrong. **Fix the type, don't widen it** — if the finding is genuinely wrong, `# pyrefly: ignore[rule-name]` on the one line with a reason, never the rule off globally. |
| `pytest -q` | the suite. Includes the differential lanes and the ported models. |
| `mkdocs build --strict` | the site. A dead cross-link, an anchor that no longer resolves, or a page under `docs/` with no `nav:` entry — see *the docs* below. |
| **bare install, at the floors** | the engine reached for something it does not declare. |

**The bare-install job is the one worth understanding.** It reinstalls with
`--resolution lowest-direct` and *no* dev group, asserts `linopy` is absent,
and runs the suite. It proves two things at once: that the relational lane
builds, solves and reads results back with no pandas, pyarrow, linopy or
xarray; and that the declared lower bounds are real rather than decorative.
Tests that need a second lane route through `tests/oracle.py`, which skips
them when it is not installed — a bare `import pandas` in a test file breaks
this job, and only this job.

Raise a floor when the code relies on that version's behaviour. Do not raise
one to chase a newer interpreter.

## The docs

`docs/` is both the site and what you read on GitHub. Write for the repo —
relative links, no site-only syntax — and the build handles the difference.

```bash
uv sync --group docs
uv run mkdocs serve  # http://127.0.0.1:8000, live-reloading
uv run mkdocs build --strict  # what CI runs
```

Three rules, each enforced, so none has to be remembered:

- **Every page under `docs/` needs a `nav:` entry** in `mkdocs.yml`. Adding a
  model page without one fails the build rather than shipping an unreachable
  page. `docs/README.md` is the deliberate exception — it is the folder view
  GitHub renders, and `exclude_docs` keeps it out of the site, where
  `docs/index.md` is the home page.
- **Inside `docs/`, link relatively.** `../SPEC.md`, `models/index.md`. mkdocs
  resolves and validates these; a dead one fails the build.
- **Outside `docs/`, write the full GitHub URL** —
  `https://github.com/FBumann/farkas/blob/main/bench/README.md`, not
  `../bench/README.md`. The site has no file above `docs/` to resolve to, and
  mkdocs does *not* flag the relative form: it ships as a silent 404. This is
  the same convention the model pages already use to link at their `.yaml`.

`tests/test_docs_site.py` enforces the last two in both directions — no
relative link may escape `docs/`, and every blob URL must name a file that
exists. Neither is checkable by mkdocs, which is why they are tests.

Headings are slugged the way GitHub slugs them, so `#track-4--sink-capabilities`
means the same thing in both places.

Read the Docs builds and publishes from `main` (`.readthedocs.yaml`); nothing
needs deploying by hand.

## Branches, commits, PRs

**Never commit on `main`.** It takes squash merges through a PR only, and the
ruleset enforces it.

The PR title is parsed by release-please and becomes the changelog entry, so it
has to be a conventional-commit subject:

```
feat: streaming executor for indexed constraints
fix(parser): where clauses with a trailing comma
refactor!: closed helper set, no monkey-patch
```

Types: `feat` `fix` `perf` `refactor` `docs` — these appear in the changelog —
plus `chore` `test` `ci` `build` `style` `revert`, which are hidden. A subject
that will not parse fails the required check rather than silently dropping the
entry. Fixing it is an edit to the PR, not a branch rewrite.

`main` is protected: no force-push, no deletion, squash-only through a PR, and
the two required checks above. Approvals are not required, but the PR is.

Versioning, the release PR, and how to force a specific version:
[RELEASING.md](RELEASING.md).

## Changing the language

**Triage first: macro, primitive, or escape?** Most requests are compositions
and cost nothing. A genuinely new shape earns a primitive only if it clears the
expressive ceiling — degree 1 ∩ relational ∩ local. Unsayable math goes to a
declared `escape:` island rather than into the language.

Read, in order:

1. [the deliberate non-primitives in docs/ROADMAP.md](docs/ROADMAP.md) — parity with
   another tool is not by itself a reason to add anything, and several
   plausible-sounding features are refused there on purpose;
2. [the ceiling in docs/design/ceiling.md](docs/design/ceiling.md#two-tiers-and-the-ceiling) —
   the admissibility test;
3. [the extension checklists](docs/ARCHITECTURE.md#extension-checklists), which sit directly under that
   test. They stay there rather than moving here: *may I?* and *how?* are one
   question, and splitting them invites answering the second without the first.

A PR that adds, renames or retires a construct updates [docs/SPEC.md](docs/SPEC.md).
Rationale belongs in the PR description or a code comment; "this used to work
differently" belongs in git.

## Adding a ported model

A port is a model somebody else already solved, said again in this language and
checked against **an optimum that did not come from us**. It is the only test
class that can catch a *shared misreading* — both lanes agreeing on a meaning
the modeller did not intend — because every other test compares farkas against
farkas. The corpus, its ladder and the ledger of what a port could *not* say
are in [docs/models/index.md](docs/models/index.md), where the reference table
is generated from `examples/ports/references.json` — the same file the tests
assert against. Each port's page there shows the model and a side-by-side
against its reference.

Four files per port:

```
examples/ports/<name>.yaml              the model
examples/ports/data/<name>.json         the instance
examples/ports/references/<name>.py     a reference implementation, importing no farkas
examples/ports/references.json          the recorded objective and where it came from
docs/models/<name>.md                   the gallery page — maths, model, side-by-side
```

- **A published optimum needs no script.** `transport_dantzig`'s number comes
  from the literature, and the citation *is* its provenance. It also ships a
  reference implementation, but as `corroborated_by` rather than as what
  verifies it — a second opinion, where the published figure is the first.
- **Reference scripts are never run by CI.** Pinning PyPSA into this project
  would hand their release cadence a veto over the suite. They carry their
  dependencies inline ([PEP 723](https://peps.python.org/pep-0723/)), pinned to
  whatever produced the recorded number, and are run out of band:
  ```bash
  uv run --script examples/ports/references/pypsa_transport.py
  ```
- **Both sides read the same instance.** A reference optimum against a
  different instance means nothing. What must stay independent is the
  formulation, not the data.
- **`rtol` is per port.** A published optimum is rounded; a solved one is not.
- **Record the duals too, if the model has any.** An objective is one number;
  a dual vector is where two implementations disagree quietly — which side of a
  constraint the price belongs to, and what sign an inequality carries. The
  reference script prints a `duals {...}` line keyed by constraint name; paste
  it into the port's entry as a `duals` block. A MILP has no dual solution, so
  it records none and the test skips rather than passing vacuously.
- **Regenerate the gallery's tables**, don't write them: both the construct
  matrix and the reference table in `docs/models/index.md` come from
  `uv run python -m tools.constructs`, and a test fails if either is stale. The
  reference table is rendered straight from `references.json`, so the published
  optimum and the asserted one cannot disagree.
- **A rung that cannot be said is also a result.** It goes in the ledger with a
  verdict — macro, primitive or escape — and feeds docs/ROADMAP.md. Do not work
  around a gap silently.

## Refreshing the benchmarks

Full method, and why each measurement is taken the way it is, in
[bench/README.md](bench/README.md). The short version:

```bash
uv run python -m bench.run --sizes xs s m l --repeat 3
uv run python -m bench.run --sizes d100 d50 d25 d08 --skip-gate --repeat 3 \
    --out bench/results/density.jsonl
uv run python -m bench.report bench/results/latest.jsonl bench/results/density.jsonl
uv run python -m bench.plot
```

Three things that have each cost us a wrong published number:

- **Measure on an idle machine.** A ladder taken while the laptop was busy
  inflated one case by 55% — enough to turn "level" into "the one case we lose".
- **A run replaces its output file.** Anything narrower than the published
  ladder goes to `--out /tmp/something.jsonl`, or the tables keep their old
  numbers with a fingerprint that no longer describes them.
- **Never retype a number.** `bench.report` prints the markdown and
  `bench.plot` rewrites the chart page's data, both from the results file. A
  figure typed by hand outlives the run that produced it.
