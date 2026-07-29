# Releasing

> **Temporary, alpha only — overrides "Normal release" below.** Until the first
> official version, nobody merges the release PR:
> [`release.yaml`](.github/workflows/release.yaml) puts it on auto-merge, so
> every merge to `main` cuts a `0.0.0aN` and a tester always has a version to
> quote. It ends by construction — the step declines any version that is not a
> prerelease — so from the first official release everything below reads as
> written. Needs "Allow auto-merge" on the repo; pause with the repo variable
> `AUTO_RELEASE=false`.

The git tag is the version. `pyproject.toml` carries no version number —
hatch-vcs derives it from the tag at build time, so nothing can drift and a
release can be cut from any branch without editing a file first.

One publish path: **pushing a `v*` tag ships it.** Every route below just
produces a tag; [`publish.yaml`](.github/workflows/publish.yaml) does the rest
(test → build → verify the built version matches the tag → GitHub release).

## Where this project is

**Alpha only, pinned there deliberately.** The project stays on the `0.0.0aN`
stream until someone edits the config; no commit, however worded, can graduate
it.

| you want | you do | version you get |
| --- | --- | --- |
| day-to-day development | nothing | `0.0.1.dev22+ged5056087` — hatch-vcs numbers every commit |
| a build someone can pin | merge the release PR | `0.0.0a1`, `0.0.0a2`, … |
| a cut from another branch | run **Prerelease** | `0.0.0a3`, or a named stream like `0.2.0rc1` |
| to leave alpha | edit the config on purpose (below) | `0.1.0` |

Untagged commits are already uniquely versioned and installable, so there is no
reason to tag until someone needs a fixed reference. `pip`/`uv` will not resolve
alphas without `--prerelease`, so they cannot be picked up by accident.

## Normal release

Land conventional commits on `main`.
[`release.yaml`](.github/workflows/release.yaml) keeps a release PR open with
the computed version and changelog. Merge it → release-please tags
`v0.0.0-alpha.N` → publish runs → dist version `0.0.0aN`. Which commit types
appear is `changelog-sections` in
[`.release-please-config.json`](.release-please-config.json); `chore`, `test`,
`ci`, `build` and `style` are hidden.

**Why the version cannot run away — and the hole that was in this claim.**
`initial-version: 0.0.0-alpha.1` matters first (without it, `release-type:
simple` falls back to release-please's default first version, **1.0.0**), and
`bump-minor-pre-major` keeps a breaking change from reaching for a major while
major is 0. `prerelease: true` also keeps the GitHub releases from showing as
"Latest".

The load-bearing one was `versioning: prerelease` + `prerelease-type: alpha`,
and it is **conditional in a way this file used to state as absolute**: a
version whose patch is 0 is an absorbing state, so patch, minor and major all
just increment the counter. `0.0.0-alpha.1 … 0.0.0-alpha.33` were immune for
exactly that reason. The stream left 0.0.0 at #251, and the immunity went with
it — on `0.0.1-alpha.12` a `feat!:` produced `0.1.0-alpha.12`, jumping a minor
by accident and resetting nothing.

So while the patch is nonzero, the pin is enforced socially rather than
arithmetically, and [`pr-title.yaml`](.github/workflows/pr-title.yaml) refuses a
`!` or a `BREAKING CHANGE:` footer to make that enforcement real. Returning the
stream to `0.0.0-alpha.N` would make the config self-enforcing again and retire
that check.

**The subject that lands on main.** `main` takes squash merges only, so one PR
is one commit and its subject is what release-please parses — the rule a
contributor has to follow, and the allowed types, are in
[CONTRIBUTING.md](CONTRIBUTING.md#branches-commits-prs). What matters here is
the failure mode: a subject release-please cannot parse breaks nothing, it just
goes silently missing from the changelog. That silence is why
[`pr-title.yaml`](.github/workflows/pr-title.yaml) is a *required* check rather
than advisory.

Because `squash_merge_commit_title` is `COMMIT_OR_PR_TITLE`, GitHub uses the PR
title on a multi-commit PR and the commit's own title on a single-commit one;
the check validates both, so neither can slip through.

## Branch protection

`main` is covered by a repository ruleset: no deletion, no force-push,
squash-only merges through a PR, and two required checks — `ci` (from
[`ci.yml`](.github/workflows/ci.yml)) and `Conventional commit subject`. The CI
job has a fixed name and no matrix, so the ruleset names it directly and never
needs updating; keep it that way. If a Python matrix ever comes back, put an
aggregating gate job in front of it rather than naming legs — requiring
`full (3.11)` would mean adding a version leaves it unrequired, and dropping
one blocks every PR on a check that can no longer report.

A required check must exist on `main` before it is required — land the workflow
first, then add it to the ruleset. Approvals are not required (solo repo); a
review count of 0 still forces the PR, the squash and the checks.

### Relaxed while in early development

Actions bills per job, rounded up to the minute, and the suite takes ~5s. So CI
cost is job count, and while the project is on the `0.0.0-alpha.N` stream — no
downstream users to break, runner minutes the scarcer resource — it is one job
that deliberately trades coverage for cost. What that gives up:

- **Only Python 3.11 is tested.** The 3.12 and 3.13 classifiers in
  `pyproject.toml` are untested claims. 3.11 is the floor, so it catches the
  common breakage (reaching for a newer stdlib feature) but not the reverse: a
  removal or deprecation that only bites on 3.13.
- **Only two dependency sets are installed:** current-with-dev, and the declared
  floors bare. The floors are exercised *without* linopy/xarray, so the linopy
  lane is only ever tested against current linopy — narrow, since the pin is
  already `>=0.9.0,<0.10`.
- **The PR-title check does not re-run on `synchronize`.** A force-push that
  leaves a PR at exactly one commit with a non-conventional subject is not
  re-validated. The cost is a missing CHANGELOG line, not a broken release.

Tighten these before the first non-alpha release — that is the point where a
missed regression reaches somebody rather than just us. A Python matrix is the
first thing to add back, behind a gate job. Until then, prefer spending minutes
on the suite over spending them on matrix breadth.

## Overriding the version

Three levers, ascending force:

1. **Edit the release PR** before merging — retitle it; release-please follows
   the PR, not just the commits.
2. **`Release-As:` footer** on any commit forces the next version:
   `git commit --allow-empty -m "chore: release 0.3.0" -m "Release-As: 0.3.0"`
3. **Tag by hand** — `git tag -a v0.3.0 -m v0.3.0 && git push origin v0.3.0`
   publishes immediately, bypassing release-please. The changelog will not
   mention it, so keep this for emergencies.

## Prereleases

Run the **Prerelease** workflow from any branch (Actions → Prerelease → Run
workflow). It computes the next counter, runs lint and the suite, and pushes the
tag; `dry-run` prints it without pushing. The defaults (`0.0.0` / `alpha`) give
the same stream release-please cuts on `main`. Once a real release is in sight,
name the version it leads to (`0.2.0`) and pick `rc`; counters are tracked per
version and channel.

Tags are dashed semver (`v0.2.0-rc.1`), which normalises to the PEP 440 dist
version `0.2.0rc1`. Do **not** hand-tag `v0.2.0rc1` — without the dash, publish
marks the GitHub release as a full release.

**On `main`, prefer the release PR.** Both routes write into the
`0.0.0-alpha.N` namespace and count independently: this workflow takes the next
free number off existing tags, while release-please counts from
[`.release-please-manifest.json`](.release-please-manifest.json), so cutting by
hand on `main` makes release-please's next number collide. Use **Prerelease**
for what it is good at — a cut from a non-`main` branch, or a differently-named
stream.

## Leaving the alpha stream

Nothing here happens by accident — you have to edit
[`.release-please-config.json`](.release-please-config.json). To cut a real
`0.1.0`: drop `versioning`, `prerelease` and `prerelease-type` (keep
`bump-minor-pre-major` unless you want 1.0.0 semantics); delete
`initial-version` (by then there is a released version to bump from, and it
would otherwise decide 1.0.0 again if the manifest is reset); and set the
version on the next release PR with a `Release-As:` footer or by retitling it.
Going to 1.0.0 is a further decision: drop `bump-minor-pre-major`.

To keep releasing 0.1.x after `main` moves to 0.2, cut a `0.1.x` branch and run
**Release** with `target-branch: 0.1.x`.

## Consuming an unreleased branch

Don't cut a release for this — install from the ref:

```bash
uv add "farkas @ git+ssh://git@github.com/FBumann/farkas@feat/some-branch"
uv add "farkas @ git+https://github.com/FBumann/farkas@d09aab6"
```

Every tagged build also attaches its wheel and sdist to the GitHub release.

## One-time setup

- **Release app** — a GitHub App with `contents: write` + `pull-requests: write`,
  credentials in secrets `APP_CLIENT_ID` / `APP_PRIVATE_KEY`. Needed so release
  PRs run CI and prerelease tags trigger publish. Without it, `release.yaml`
  degrades to `GITHUB_TOKEN` and warns; `prerelease.yaml` refuses to run.
- **PyPI** — currently off. The `pypi` job is skipped unless the repo variable
  `PUBLISH_TO_PYPI` is `true`. To go live: register a
  [trusted publisher](https://docs.pypi.org/trusted-publishers/) for
  `farkas` (workflow `publish.yaml`, environment `pypi`), create the `pypi`
  environment, then set the variable.
