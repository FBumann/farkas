# Releasing

The git tag is the version. `pyproject.toml` carries no version number —
hatch-vcs derives it from the tag at build time, so nothing can drift and a
release can be cut from any branch without editing a file first.

One publish path: **pushing a `v*` tag ships it.** Every route below just
produces a tag; [`publish.yaml`](.github/workflows/publish.yaml) does the rest
(test → build → verify the built version matches the tag → GitHub release).

## Where this project actually is

**Alpha only, and pinned there deliberately.** This project stays on the
`0.0.0aN` stream until someone decides otherwise in a config change — no commit,
however it is worded, can graduate it:

| you want | you do | version you get |
| --- | --- | --- |
| day-to-day development | nothing | `0.0.1.dev22+ged5056087` — hatch-vcs numbers every commit automatically |
| a build someone can pin | merge the release PR | `0.0.0a1`, `0.0.0a2`, … |
| a cut from some other branch | run **Prerelease** | `0.0.0a3`, or a named stream like `0.2.0rc1` |
| to leave alpha | edit the config on purpose — see [Leaving the alpha stream](#leaving-the-alpha-stream) | `0.1.0` |

Untagged commits are already uniquely versioned and installable, so there is no
reason to tag until someone needs a fixed reference. The alpha stream exists for
exactly that: pinning `0.0.0a3` in a downstream project beats pinning a branch
that moves under you. `pip`/`uv` will not resolve alphas without `--prerelease`,
so they cannot be picked up by accident.

## Normal release

Land conventional commits on `main`. [`release.yaml`](.github/workflows/release.yaml)
keeps a release PR open with the computed version and changelog. Merge it →
release-please tags `v0.0.0-alpha.N` → publish runs → dist version `0.0.0aN`.

Which commit types appear in the changelog is `changelog-sections` in
[`.release-please-config.json`](.release-please-config.json); `chore`, `test`,
`ci`, `build` and `style` are hidden today.

### Why the version cannot run away

Three settings in [`.release-please-config.json`](.release-please-config.json)
interlock, and it takes all three:

- `initial-version: 0.0.0-alpha.1` — the first release. Without it, `release-type:
  simple` falls back to release-please's built-in default first version, which is
  **1.0.0**. (That is not a bug in the config; there is simply no prior tag to bump
  from, and `bump-minor-pre-major` only governs bumps from an existing 0.x version.)
- `versioning: prerelease` + `prerelease-type: alpha` — bumps move the `alpha.N`
  counter instead of the numbers. In this strategy a version whose **patch is 0** is
  an absorbing state: patch, minor *and* major release types all just increment the
  prerelease counter. `0.0.0-alpha.N` therefore has nowhere to go but `alpha.N+1`.
- `bump-minor-pre-major` — belt and braces: while major is 0, a breaking change is
  a *minor* bump, so `feat!:` cannot reach for a major either.

The net effect: **no commit message can bump the major, or the minor, or the
patch.** Leaving alpha is a deliberate edit to this file, never a side effect of
landing a commit.

`prerelease: true` also marks the GitHub releases as prereleases, so they never
show up as "Latest".

### The subject that lands on main

`main` takes squash merges only, so one PR is one commit and its subject is what
release-please parses. A subject it cannot parse does not fail anything — the entry
is just silently missing from the changelog. [`pr-title.yaml`](.github/workflows/pr-title.yaml)
is what catches that, and it is a required check:

```
<type>[(scope)][!]: <subject>

feat: streaming executor for indexed constraints
fix(parser): where clauses with a trailing comma
refactor!: closed helper set, no monkey-patch
```

Types are the ones in `changelog-sections`, plus `revert`. Because
`squash_merge_commit_title` is `COMMIT_OR_PR_TITLE`, GitHub uses the PR title on a
multi-commit PR and the commit's own title on a single-commit one — the check
validates both, so whichever GitHub picks has been vetted. Fixing a title is an edit
to the PR, not a branch rewrite; the check re-runs on edit.

## Branch protection

`main` is covered by a repository ruleset (Settings → Rules): no deletion, no
force-push, squash-only merges through a PR, and these required checks:

| check | comes from |
| --- | --- |
| `CI ok` | [`ci.yml`](.github/workflows/ci.yml) — aggregates `native` and the `full` matrix |
| `Conventional commit subject` | [`pr-title.yaml`](.github/workflows/pr-title.yaml) |

`CI ok` exists so the ruleset never names the matrix legs directly. Requiring
`full (3.11)` would mean adding a Python version leaves it unrequired, and dropping
one blocks every PR on a check that can no longer report. Change the matrix freely;
the ruleset only knows about `CI ok`.

Approvals are not required — it is a solo repo, and a review count of 0 still forces
the PR, the squash and the checks. Raise `required_approving_review_count` when that
changes.

A required check must exist on `main` before it is required, or every PR waits on a
check that never reports. Land the workflow first, then add it to the ruleset.

## Overriding the version

Three levers, in ascending order of force:

1. **Edit the release PR** before merging — retitle it to the version you want;
   release-please follows the PR, not just the commits.
2. **`Release-As:` footer** on any commit forces the next version outright:

   ```
   git commit --allow-empty -m "chore: release 0.3.0" -m "Release-As: 0.3.0"
   ```

3. **Tag by hand.** `git tag -a v0.3.0 -m v0.3.0 && git push origin v0.3.0`
   publishes immediately, bypassing release-please. The changelog will not
   mention it, so keep this for emergencies.

## Prereleases

Run the **Prerelease** workflow from any branch (Actions → Prerelease → Run
workflow → pick the branch). It computes the next counter, runs lint and the
suite, and pushes the tag. `dry-run` prints the tag without pushing.

The defaults (`0.0.0` / `alpha`) give the same alpha stream release-please cuts on
`main` — `v0.0.0-alpha.1`, `-alpha.2`, … Once a real release is in sight, name the
version it leads to (`0.2.0`) and pick `rc`; counters are tracked per version and
channel, so `v0.2.0-rc.1` starts fresh regardless of how many alphas came before.

Tags are dashed semver (`v0.2.0-rc.1`), which normalises to the PEP 440 dist
version `0.2.0rc1`. Do **not** hand-tag `v0.2.0rc1`: without the dash, publish
marks the GitHub release as a full release.

**On `main`, prefer the release PR.** Both routes now write into the `0.0.0-alpha.N`
namespace, and they count independently: this workflow takes the next free number
off the existing tags, while release-please counts from
[`.release-please-manifest.json`](.release-please-manifest.json). Cutting by hand on
`main` makes release-please's next number collide with a tag that already exists.
Use **Prerelease** for what it is good at — a cut from a branch that is not `main`,
or a differently-named stream (`0.2.0` / `rc`).

Prereleases need the release app installed (see setup below), because a tag
pushed with `GITHUB_TOKEN` does not trigger `publish.yaml`. Without it, tag
locally instead — a human-pushed tag triggers publish fine.

## Leaving the alpha stream

Nothing here happens by accident — you have to come and edit
[`.release-please-config.json`](.release-please-config.json). To cut a real `0.1.0`:

1. Drop `versioning`, `prerelease` and `prerelease-type` from the package config.
   Keep `bump-minor-pre-major` unless you actually want 1.0.0 semantics.
2. Delete `initial-version` — by then there is a released version to bump from, so
   it is dead weight (and it would otherwise be the thing that decides 1.0.0 again
   if the manifest is ever reset).
3. Set the version you want on the next release PR with a `Release-As:` footer, or
   just retitle the PR. Landing on `0.1.0` rather than whatever the commits imply is
   usually the point.

Going to 1.0.0 is a further, separate decision: drop `bump-minor-pre-major` so
`feat!:` bumps the major again.

## Maintenance branches

To keep releasing 0.1.x after `main` has moved to 0.2: cut a branch (`0.1.x`),
then run **Release** with `target-branch: 0.1.x`. release-please maintains a
separate release PR and changelog for that line.

## Consuming an unreleased branch

Don't cut a release for this. Install from the ref:

```bash
uv add "linopy-yaml @ git+ssh://git@github.com/FBumann/linopy-yaml@feat/some-branch"
uv add "linopy-yaml @ git+https://github.com/FBumann/linopy-yaml@d09aab6"   # or pin a sha
```

Every tagged build also attaches its wheel and sdist to the GitHub release, so
those are installable without PyPI.

## One-time setup

- **Release app** — a GitHub App with `contents: write` + `pull-requests: write`,
  its credentials in repo secrets `APP_CLIENT_ID` / `APP_PRIVATE_KEY`. Needed so
  release PRs run CI and prerelease tags trigger publish. Without it, `release.yaml`
  degrades to `GITHUB_TOKEN` and warns; `prerelease.yaml` refuses to run.
- **PyPI** — currently off. The `pypi` job is skipped unless the repo variable
  `PUBLISH_TO_PYPI` is `true`. To go live: register a
  [trusted publisher](https://docs.pypi.org/trusted-publishers/) for `linopy-yaml`
  (workflow `publish.yaml`, environment `pypi`), create the `pypi` environment in
  repo settings, then set the variable. The name is unclaimed as of this writing.
