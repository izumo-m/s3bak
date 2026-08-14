# Exclusion model (`excludes`)

An entry's `excludes` name the paths s3bak must ignore. This document defines
what "excluded" means to the implementation and what exclusion does to the
manifest; [sync.md](sync.md) describes where the filter sits in each pipeline,
and the user-facing story — the pattern language with examples, and what each
command shows — is the manual's
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
previews exactly that cleanup. `verify` reports the residue (see
[verify.md](verify.md)), because with every other command ignoring it, verify
is the one passive channel through which it can be discovered. None of these
touches the local side.

## Pattern language

The pattern language is aws-cli's `--exclude`, provided by boto3-s3's
`globsieve` engine — s3bak delegates the matching rather than reimplementing
it, so the two cannot drift. What `aws s3 sync --exclude P` excludes, s3bak
excludes; what it does not, s3bak does not. That delegation is the design
decision; the properties below are the ones s3bak's own code must not break
when it hands a path to the engine.

- A **relative pattern** is matched against the whole path relative to the
  **entry root**, both ends anchored (fnmatch). The anchor is the entry root
  in every scope, including a sub-path push, which re-anchors its walk keys
  rather than matching them against the sub.
- An **absolute pattern** (`/...`; on Windows also `C:/...` and UNC forms) is
  matched against the path's absolute local form — the entry tree for a push,
  the restore destination for a pull. It never matches an S3-side key, which
  carries no anchor.
- On Windows, `\` in a pattern folds to `/` and a drive-relative `C:foo`
  anchors to the root as `foo`, exactly as `globsieve` documents.
- The engine is last-match-wins over include/exclude rules; s3bak's config
  carries excludes only, so the list degenerates to "excluded iff any pattern
  matches" and order does not matter.

## Every path is judged alone

Matching is per path, with no propagation in either direction: excluding a
directory does not exclude its children, and excluding every child does not
exclude the directory. A directory is matched with a trailing `/` on its key,
a file or symlink without one — so `cache/*` covers a whole subtree only
because `*` matches each descendant key on its own, never through a
propagation rule. The key shape is therefore part of the contract: a **symlink
named `cache` is not covered by `cache/` or `cache/*`**, because its key
carries no trailing slash, however directory-like the link looks.

Consequences the implementation must preserve:

- **The entry root is never matched.** In aws terms the operation root has no
  key, and filters apply beneath it. A single-file entry's file *is* the entry
  root, so excludes never apply to it.
- **Pruning is an optimization only.** Skipping the descent into a directory
  is permitted where the pattern set provably excludes the directory and
  everything below it (the `dir/*` shape); it must never change what the rules
  above decide.

## Where the filter sits

The exclude predicate (`Excludes`) is one module with no s3bak dependencies,
so every layer that must agree on what an exclude means shares it. Beyond the
plain "invisible" rule, these are the seams worth stating:

- **push** filters the local side of the sync only; the S3 listing is never
  filtered. That is what leaves residue under excluded paths in view rather
  than hiding it forever: an object still paired with its record is kept, a
  stale record without an object is dropped by any push, and under `--delete`
  both become ordinary candidates.
- **a sub-path push** treats a named path the filter leaves empty as a locally
  missing one, with one difference: ignoring is the rule, not an error, so
  without `--delete` the push does nothing and exits 0 where a missing,
  non-excluded sub-path is an error. When a path is both excluded and locally
  missing, exclusion wins.
- **pull** skips excluded records entirely, and `pull --delete` never sees an
  excluded local path as an extra. A restored file whose parent directory is
  excluded — and hence unrecorded — gets that directory created as a plain
  container: default permissions, no recorded metadata, unmanaged by s3bak.
- **verify** keeps its listing checks exclude-blind, since residue pairs are
  internally consistent and pass them, and adds the residue count as a
  separate warning. The `--checksum` content comparison skips residue records:
  its remedies cannot touch an excluded path.

## Manifest consequences

- **An excluded directory has no record.** Its visible children are recorded
  normally, so a record's parent directory record is optional: the validator
  must not treat a missing parent as damage
  ([manifest.md](manifest.md#robustness)). Validation cannot condition this on
  the excludes in force — a manifest is read under whatever configuration the
  reader has — so parents are optional unconditionally.
- **An unrecorded object under an excluded path cannot be adopted by a push.**
  A record is a stat snapshot of a local file, and the local side is invisible
  by rule, so there is nothing truthful to record. It is the one persistent
  exception to the manifest's correspondence with S3
  ([storage.md](storage.md#unrecorded-objects)); lifting the exclude and
  pushing adopts it, `push --delete` retires it.
