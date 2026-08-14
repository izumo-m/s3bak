# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project follows [Semantic Versioning](https://semver.org/). While below
1.0.0, MINOR marks a spec change (Added / Changed / Removed) and PATCH marks
a fix only (Fixed).

## [Unreleased]

### Added

- `verify` now reports excluded residue: records still sitting under the
  entry's current `excludes` — the leftovers of an exclude added after the
  paths were pushed — warn with one count per entry (exit 2), naming
  `push --delete` as the remedy. With every other command ignoring
  excluded paths, this is the one passive channel that surfaces them; the
  integrity checks themselves stay exclude-blind, and an unrecorded object
  under an excluded path keeps its own unrecorded-object warning.
- `status --delete`: the preview of `push --delete`. Manifest-only records —
  locally deleted paths and residue under excluded paths alike — print as
  `D`, the candidates the push would offer. Like plain status it never
  lists the bucket, so it previews only what the manifest records; the
  exact rehearsal with the real listing stays `push --delete --dry-run`.
- `s3bak hook pre <entry>` / `s3bak hook post <entry>`: run one configured
  hook on demand, outside any push — re-running an off-site copy after the
  far side changed, or testing a dump script. The hook executes under the
  same contract as a push-run hook (argument vector, no shell, stdin
  detached, the same exit-status normalization), with `S3BAK_JOURNAL`
  unset. An entry NAMED without the hook configured fails (exit 1) — the
  config ignores misspelled keys silently, so this error is where a
  `post_hok:` typo surfaces; `--all` instead runs every configured hook of
  that kind, skipping hook-less entries (shown under `-v`), and errors
  only when no entry configures one. `--dry-run` prints the command line
  without running it; like `list`, the command needs no S3 client.

### Removed

- The migration hint for `~/.config/s3bak/config.sh`, printed instead of the
  ordinary "config file not found" message when that file existed. s3bak has
  read `config.py` for its whole documented life, and the skeleton the plain
  message offers is more useful than a pointer to a format nothing describes.
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

- Plain `status` prints no `D`: each status variant previews its push, and
  a plain push touches nothing at a manifest-only key, so plain status says
  nothing there — `D` is `status --delete`'s letter now. A type-changed
  pair (a recorded file replaced by a local symlink, and the reverse)
  reports `M` with a `type` tag instead of `D`: a plain push acts on it by
  re-recording the kind.
- Without `--delete`, an excluded path is now ignored in full, on both
  sides: push neither uploads nor deletes it and records nothing about it,
  and pull neither restores, overwrites, nor touches its metadata — a path
  excluded after it was backed up stays deleted when deleted locally, and
  stays as it is when present. Naming an excluded path no longer overrides
  the exclude: a sub-path push or pull of a path the filter leaves nothing
  visible at does nothing and exits 0, and `push --delete` of such a path
  removes its backup behind the one-question subtree confirmation — the
  supported way to retire it by name.
- `excludes` now match exactly like `aws s3 sync --exclude` — the engine is
  boto3-s3's own, so the two cannot drift. Every path is judged alone
  against its whole entry-rooted key, with a directory carrying a trailing
  `/` on its key: `cache/*` still covers the directory and its whole
  subtree, `cache/` matches the directory entry alone (its contents stay
  backed up), and a bare `cache` matches only a file or symlink of that
  name — exactly what aws-cli would exclude, where previously such patterns
  were silently ignored. An absolute pattern (`/...`, or a drive letter on
  Windows) now matches against the absolute local path. A symlink is no
  longer covered by a directory pattern of its name. An excluded directory
  is no longer recorded, so a record's parent directory record is now
  optional in the manifest — a missing parent is a valid manifest, not
  damage.
- A pull that meets a file record whose S3 object is gone — the residue of
  an interrupted deletion or an out-of-band delete — now warns, skips the
  record in full, and restores everything else (exit 2), instead of failing
  the whole restore over residue the next push retires anyway. Skipped in
  full means the local path is left exactly as it is: no download, and no
  metadata applied — stamping the record's mode/mtime over content that was
  never restored would report a restore that did not happen and hide a
  diverged local copy from every later size+mtime comparison. A missing
  special-file record stays a hard error (pull never creates one).
- A manifest file record whose S3 object is gone is now retired by any push,
  not only by `push --delete`: it describes a backup that no longer exists,
  so dropping it is repair rather than deletion and is done silently. A
  record whose object is still on S3 is untouched, as before.
- Answering `q` to a `--delete` confirmation now reports what the abort left
  behind and how to settle it — for a push, that the manifest was not
  rewritten and may no longer match S3; for a pull, that the local tree was
  updated only as far as the answers went. Under `--all` the entries the
  abort stopped before are named instead of being skipped silently.
- The boto3-s3 requirement is now `>=0.8,<0.11`, so a fresh install resolves
  to 0.10 while a project already holding 0.8 can still take s3bak alongside
  it.
- An entry path with no parent is now refused as a "path root" rather than a
  "filesystem root". The check has always been on the path alone, so the old
  wording suggested a mount point was refused too, which it never was.
- A single-file entry's pull now prints its `download:` line, naming the
  transfer path it took — `(boto3-s3 cp)` for an object at or above the
  multipart threshold, `(boto3 get_object)` below it. A directory pull's
  lines come from boto3-s3, which reports its own transfers; nothing
  reported this one, so a real run was silent where its `--dry-run` had
  announced a download. That dry-run line now carries the same lane and the
  `to` spelling the sync's lines use.

### Fixed

- A `--delete` confirmation is no longer scrolled away by the transfer result
  lines of the same run: the question holds the terminal until it is
  answered, and the output that arrives meanwhile prints afterwards.
- `show` now says why it cannot print something instead of leaking the
  storage service's `NoSuchKey` text: a directory or a symlink has no stored
  content, a record whose object is gone is named as the stale residue it is,
  and a path the backup does not hold is reported as not found. The manifest
  that knows the difference is fetched only after the stream has already
  failed, so the ordinary case is still one request — and `show` remains the
  one command that works while a manifest is damaged, reporting the bare
  absence when the manifest cannot explain it.

## [0.5.0] - 2026-07-31

### Added

- Added this CHANGELOG.md to track notable changes going forward.
