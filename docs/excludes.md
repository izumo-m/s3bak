# Exclusion model (`excludes`)

An entry's `excludes` name the paths s3bak must ignore. This document defines
the pattern language, what "excluded" means to each command, and what
exclusion does to the manifest. [sync.md](sync.md) describes where the filter
sits in each pipeline; the user-facing story is the manual's
[change-detection chapter](manual/04-change-detection.md).

## The rule

Without `--delete`, an excluded path does not exist as far as s3bak is
concerned — locally or on S3, present or not. Push neither uploads nor
deletes it and records nothing about it; pull neither restores, overwrites,
nor deletes it, and applies no metadata to it. Exclusion is conceptually a
scan-side filter: wherever s3bak instead applies it at the compare (the S3
listing and the manifest are always read complete), that is an implementation
seam serving manifest consistency, not a difference in meaning.

The deliberate exemptions: `push --delete` is the operator's instruction to
retire backup residue — objects and records pushed before the exclude was
added are deletion candidates like any other (see
[sync.md](sync.md#deleting-backups---delete---yes)) — and `status --delete`
previews exactly that cleanup, so it shows the same residue as `D`.
`verify` reports the residue (see [verify.md](verify.md)), because with
every other command ignoring it, verify is the one passive channel through
which it can be discovered. None of these touches the local side.

## Pattern language

The pattern language is aws-cli's `--exclude`, provided by boto3-s3's
`globsieve` engine — s3bak delegates the matching rather than reimplementing
it, so the two cannot drift. What `aws s3 sync --exclude P` excludes, s3bak
excludes; what it does not, s3bak does not.

- A **relative pattern** is matched against the whole path relative to the
  **entry root**, both ends anchored (fnmatch): `*` matches across `/`, `?`
  matches one character, `[...]` is a character class. `*.elc` matches at
  every depth; `__pycache__/*` matches only under the entry-root
  `__pycache__`.
- An **absolute pattern** (`/...`; on Windows also `C:/...` and UNC forms) is
  matched against the path's absolute local form — the entry tree for a
  push, the restore destination for a pull — aws-cli's join-onto-root
  semantics. It never matches an S3-side key, which carries no anchor.
- On Windows, `\` in a pattern folds to `/` and a drive-relative `C:foo`
  anchors to the root as `foo`, exactly as `globsieve` documents.
- The engine is last-match-wins over include/exclude rules; s3bak's config
  carries excludes only, so the list degenerates to "excluded iff any
  pattern matches" and order does not matter.

## Every path is judged alone

Matching is per path, with no propagation in either direction:

- A **directory** is matched with a trailing `/` on its key. `cache/`
  matches the directory `cache` and nothing beneath it; `cache/*` matches
  the directory itself (`*` may match the empty tail) and every descendant,
  each on its own key — which is why it excludes the whole subtree without
  any propagation rule. A bare `cache` matches a **file or symlink** named
  `cache` and no directory, exactly as in aws-cli, where directories are not
  things.
- Excluding a directory does not exclude its children (`cache/` leaves every
  path under `cache/` in the backup), and excluding every child does not
  exclude the directory.
- A symlink is excluded only when its own key matches. A symlink named
  `cache` is not covered by `cache/` or `cache/*` — its key is `cache`,
  without the trailing slash.
- The **entry root is never matched**: in aws terms the operation root has
  no key, and filters apply beneath it. `*` excludes everything under the
  entry, never the entry itself. A single-file entry's file is the entry
  root, so excludes never apply to it.

Skipping the descent into a directory ("pruning") is an optimization,
permitted only where the pattern set provably excludes the directory and
everything below it (the `dir/*` shape); it must never change what the rules
above decide.

## What each command does

- **push** — an excluded local path is invisible: not uploaded, not
  recorded. The S3 listing is never filtered, so residue under excluded
  paths stays in view: a record whose object is still there is kept (the
  pair travels together), a stale record with no object is dropped by any
  push (repair, not deletion), and under `--delete` the residue objects and
  records are ordinary candidates.
- **sub-path push** — patterns keep their entry-root anchor in every scope,
  and naming an excluded path does not override the exclude. A named
  sub-path where the filter leaves nothing visible is treated exactly like a
  locally missing one, with one difference in the no-`--delete` case:
  ignoring is not an error, so the push does nothing and exits 0, while a
  missing, non-excluded sub-path stays the existing error. With `--delete`,
  both offer the backup at the named path as one confirmed subtree
  deletion. When a path is both excluded and locally missing, exclusion
  wins.
- **pull** — a record whose path is excluded is skipped: no download, no
  recreation, no metadata; the local path, present or not, is untouched.
  `pull --delete` never removes an excluded local path — it is invisible to
  the extras diff. A restored file whose parent directory is excluded (and
  hence unrecorded) gets that directory created as a plain container:
  default permissions, no recorded metadata, unmanaged by s3bak.
- **status / diff** — an excluded local path never compares and never
  reports `A`; plain `status` prints nothing for any manifest-only record,
  excluded or not (a plain push touches nothing at their keys).
  `status --delete`, the preview of `push --delete`, shares its exemption:
  residue under excluded paths prints as `D`. Passive discovery stays
  `verify`'s job.
- **verify** — the reporting exemption. The listing checks stay
  exclude-blind — residue pairs are internally consistent and pass them —
  and verify additionally warns (exit 2) when records remain under the
  entry's current excludes, one count per entry, naming `push --delete` as
  the remedy ([verify.md](verify.md)). An unrecorded object under an
  excluded path is reported like any other. The opt-in `--checksum` content
  comparison skips residue records: its remedies cannot touch an excluded
  path, and the residue warning already points at the pair.

## Manifest consequences

- **An excluded directory has no record.** Its visible children are recorded
  normally, so a record's parent directory record is optional: the validator
  must not treat a missing parent as damage
  ([manifest.md](manifest.md#robustness)). Pull creates the missing levels
  as plain directories when a restored child needs them.
- **An unrecorded object under an excluded path cannot be adopted by a
  push.** A record is a stat snapshot of a local file, and the local side is
  invisible by rule, so there is nothing truthful to record. `verify`
  reports the object until a `push --delete` retires it — the one persistent
  exception to the manifest's correspondence with S3
  ([storage.md](storage.md#unrecorded-objects)); lifting the exclude and
  pushing adopts it instead.
