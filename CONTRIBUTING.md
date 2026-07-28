# Contributing

Procedure lives here. **Why** the project is shaped the way it is lives in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), and that split is deliberate: this file
should be readable in one sitting and go stale only when a command changes.

## Setup

```bash
uv sync                  # dev group is the default: tools + the linopy oracle
uv run pre-commit install  # once per clone
```

`uv sync` installs the `[linopy]` extra too, because the differential test
suite needs a second lane to compare against. The engine itself never imports
linopy, xarray or pandas — see *the bare install* below.

## The loop

```bash
uv run pytest -q                 # ~20 s
uv run ruff check --fix . && uv run ruff format .
uv run pyrefly check
```

Narrower runs while working:

```bash
uv run pytest tests/test_relational.py -q
uv run pytest -k piecewise -q
uv run pytest --lf                # last failures only
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
2. [the ceiling in docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#two-tiers-and-the-ceiling) —
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
farkas. The corpus and its ladder are in [docs/ports.md](docs/ports.md); each port's
page in [the gallery](docs/models/index.md) shows the model and a side-by-side
against its reference.

Four files per port:

```
examples/ports/<name>.yaml              the model
examples/ports/data/<name>.json         the instance
examples/ports/references/<name>.py     a reference implementation, importing no farkas
examples/ports/references.json          the recorded objective and where it came from
docs/models/<name>.md                   the gallery page — maths, model, side-by-side
```

- **A published optimum needs no script.** `transport_dantzig` has none: the
  number came from the literature. Record the citation as its provenance.
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
