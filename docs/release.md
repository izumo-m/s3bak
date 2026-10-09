# Release procedure

`main` carries released versions only: it moves by merging `develop` at a
release, and the annotated tag `vX.Y.Z` sits on that merge. After the release
`develop` is fast-forwarded to `main`, so `develop` always contains `main`,
and a release that was never merged shows up as a `main` that `develop` does
not contain.

Steps 2 to 6 write to the public repository; confirm before running them.

1. On `develop`, commit the version bump:
   - turn `[Unreleased]` in CHANGELOG.md into `## [X.Y.Z] - YYYY-MM-DD`;
   - raise `version` in `pyproject.toml` and in `uv.lock`, and check the
     lock file with `uv lock --check`.
2. On `main`, merge `develop` with a merge commit: `git merge --no-ff develop`.
3. Tag the merge result: `git tag -a vX.Y.Z -m vX.Y.Z`.
4. On `develop`, fast-forward to `main`: `git merge --ff-only main`.
5. Push: `git push origin main develop vX.Y.Z`.
6. Create the GitHub Release: `scripts/gh-release.sh vX.Y.Z`. The notes are
   that version's CHANGELOG section, read from the tag. A tag that already
   has a Release is skipped, and `--dry-run` prints the notes without
   creating anything. Tags up to v0.9.0 have no Release and are not
   backfilled.
