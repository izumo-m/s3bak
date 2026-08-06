# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project follows [Semantic Versioning](https://semver.org/). While below
1.0.0, MINOR marks a spec change (Added / Changed / Removed) and PATCH marks
a fix only (Fixed).

## [Unreleased]

### Removed

- `--meta-only` and `--data-only`, on push and pull both. A one-sided push
  could record local state S3 does not hold (`--meta-only`, which then hides
  a never-pushed edit from every later size+mtime check) or upload objects
  the manifest does not record (`--data-only`) — both broke the manifest's
  correspondence with S3, and the journal-driven push refreshes metadata and
  records uploads in the same scan, leaving them no job. A one-sided pull
  restored data without its recorded metadata, or the reverse — a tree no
  record describes. Re-running a `post_hook` on demand, the one job left to
  `push --meta-only`, is the `hook` command added in this release.

### Changed

- A manifest file record whose S3 object is gone is now retired by any push,
  not only by `push --delete`: it describes a backup that no longer exists,
  so dropping it is repair rather than deletion and is done silently. A
  record whose object is still on S3 is untouched, as before.
- Answering `q` to a `--delete` confirmation now reports what the abort left
  behind and how to settle it — for a push, that the manifest was not
  rewritten and may no longer match S3; for a pull, that the local tree was
  updated only as far as the answers went. Under `--all` the entries the
  abort stopped before are named instead of being skipped silently.
- The boto3-s3 requirement is now `>=0.8,<0.10`, so a fresh install resolves
  to 0.9 while a project already holding 0.8 can still take s3bak alongside
  it.
- An entry path with no parent is now refused as a "path root" rather than a
  "filesystem root". The check has always been on the path alone, so the old
  wording suggested a mount point was refused too, which it never was.

### Fixed

- A `--delete` confirmation is no longer scrolled away by the transfer result
  lines of the same run: the question holds the terminal until it is
  answered, and the output that arrives meanwhile prints afterwards.

## [0.5.0] - 2026-07-31

### Added

- Added this CHANGELOG.md to track notable changes going forward.
