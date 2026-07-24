# Releasing

The git tag is the version. `pyproject.toml` carries no version number —
hatch-vcs derives it from the tag at build time, so nothing can drift and a
release can be cut from any branch without editing a file first.

One publish path: **pushing a `v*` tag ships it.** Every route below just
produces a tag; [`publish.yaml`](.github/workflows/publish.yaml) does the rest
(test → build → verify the built version matches the tag → GitHub release).

## Where this project actually is

Pre-1.0 and unpublished, so most of the time you want the first row and nothing else:

| you want | you do | version you get |
| --- | --- | --- |
| day-to-day development | nothing | `0.0.1.dev22+ged5056087` — hatch-vcs numbers every commit automatically |
| a build someone can pin | run **Prerelease** (defaults) | `0.0.0a1`, `0.0.0a2`, … |
| an actual release | merge the release PR | `0.1.0` |

Untagged commits are already uniquely versioned and installable, so there is no
reason to tag until someone needs a fixed reference. The alpha stream exists for
exactly that: pinning `0.0.0a3` in a downstream project beats pinning a branch
that moves under you. `pip`/`uv` will not resolve alphas without `--prerelease`,
so they cannot be picked up by accident.

Release-please keeps a release PR open on `main` the whole time. Leave it unmerged —
it doubles as a live draft of what the first real release would contain, and
merging it is the go-live button. (To silence it until you want it, drop
`push: branches: [main]` from `release.yaml` and run it from Actions instead.)

## Normal release

Land conventional commits on `main`. [`release.yaml`](.github/workflows/release.yaml)
keeps a release PR open with the computed version and changelog. Merge it →
release-please tags → publish runs.

Which commit types appear in the changelog is `changelog-sections` in
[`.release-please-config.json`](.release-please-config.json); `chore`, `test`,
`ci`, `build` and `style` are hidden today.

Pre-1.0, `bump-minor-pre-major` means `feat!:` bumps the minor, not the major.

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

The defaults (`0.0.0` / `alpha`) give the pre-1.0 alpha stream — `v0.0.0-alpha.1`,
`-alpha.2`, … Once a real release is in sight, name the version it leads to
(`0.2.0`) and pick `rc`; counters are tracked per version and channel, so
`v0.2.0-rc.1` starts fresh regardless of how many alphas came before.

Tags are dashed semver (`v0.2.0-rc.1`), which normalises to the PEP 440 dist
version `0.2.0rc1`. Do **not** hand-tag `v0.2.0rc1`: without the dash, publish
marks the GitHub release as a full release.

This is deliberately not release-please's `prerelease` config, which is sticky —
once enabled, every release on that branch stays an rc until the config changes
back.

Prereleases need the release app installed (see setup below), because a tag
pushed with `GITHUB_TOKEN` does not trigger `publish.yaml`. Without it, tag
locally instead — a human-pushed tag triggers publish fine.

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
