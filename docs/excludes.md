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

The pattern language is aws-cli's `--exclude` / `--include`, provided by
boto3-s3's `globsieve` engine — s3bak delegates the matching rather than
reimplementing it, so the two cannot drift. What `aws s3 sync --exclude P`
excludes, s3bak excludes; what `--include P` takes back, s3bak takes back;
what they do not, s3bak does not. That delegation is the design decision;
the properties below are the ones s3bak's own code must not break when it
hands a path to the engine.

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
- A pattern prefixed with `!` is an include (aws-cli's `--include`). The
  engine is last-match-wins over the list in config order: an unmatched key
  is included, and the last pattern that matches a key decides. A list with
  no `!` degenerates to "excluded iff any pattern matches", where order does
  not matter; with includes, order is the whole point — `["cache/*",
  "!cache/keep/*"]` carves `keep` back out, and the reverse order does not.
  The `!` spelling is s3bak's (aws-cli has separate flags), and it is the
  only thing that is: what follows it is matched exactly as `--include`
  would. A name that begins with a literal `!` is matched by a character
  class (`[\!]name`).
- Config validation rejects the shapes that cannot mean what they say: an
  empty pattern, a bare `!`, and a list that opens with an include —
  everything is included until an exclude matches, so a leading include
  takes nothing back, the aws-cli trap of a lone `--include`.

## Every path is judged alone

Matching is per path, with no propagation in either direction: excluding a
directory does not exclude its children, and excluding every child does not
exclude the directory. A directory is matched with a trailing `/` on its key,
a file or symlink without one — so `cache/*` covers a whole subtree only
because `*` matches each descendant key on its own, never through a
propagation rule. The key shape is therefore part of the contract: a **symlink
named `cache` is not covered by `cache/` or `cache/*`**, because its key
carries no trailing slash, however directory-like the link looks.

Includes propagate no more than excludes do: `!docs/*.md` takes back the
`.md` files and no directory key, so under a catch-all `*` the directories
they sit in stay excluded and unrecorded, and a pull creates them as plain
containers. Recording them is the operator's explicit `!*/` — every
directory key ends in `/`, which `*` reaches — the rsync idiom. An implicit
rule, "a directory is included when a descendant is", was rejected: the
manifest emits `docs/` before anything under it, so deciding it would need
the whole subtree in hand, which the streaming invariant
([overview.md](overview.md#performance-and-scalability)) forbids. The static
variant, "when a later include *could* match beneath it", is decidable per
path but records directories nothing was taken back under, and departs from
aws-cli for no gain over the idiom.

Consequences the implementation must preserve:

- **The entry root is never matched.** In aws terms the operation root has no
  key, and filters apply beneath it. A single-file entry's file *is* the entry
  root, so excludes never apply to it.
- **Pruning is an optimization only.** Skipping the descent into a directory
  is permitted only where the pattern list provably excludes the directory
  and everything below it; it must never change what the rules above decide.
  The proof is shape-based. A relative `P*` pattern (`dir/*`, or the
  catch-all `*`) matches exactly the keys that start with `P`, so the last
  such pattern covering the directory's key decides its whole subtree: the
  subtree is pruned iff that pattern is an exclude and no later include
  could match beneath the directory. "Could match" is judged by the
  include's literal head, the text before its first wildcard — a key it
  matches starts with that head, so it reaches under the directory only if
  one of the two is a prefix of the other, and a wildcard-free include, which
  matches its head alone, only if the head lies under the directory. An
  absolute include is first joined onto the directory's absolute path
  exactly as the engine joins it onto every key beneath (on Windows the join
  lends the directory's drive to a driveless pattern), and the joined
  pattern's head is judged against that path the same way. This is what
  keeps a `$HOME` entry that takes back `.config/nvim/*` from walking
  `Downloads`; an include that can reach anywhere (`!*.md`, `!*/`) blocks
  every prune, and the whole tree is walked.

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
  missing, exclusion wins — but a path absent locally has no kind of its own
  to judge, so what the backup records under it is judged instead, each
  record alone by its recorded kind: exclusion wins only when every record
  there is excluded, or, with nothing recorded, when the name is excluded in
  either spelling (as a file or as a directory). The record-by-record rule is
  what keeps `["*", "!x/*"]` from silencing a `push entry/x` whose backup is
  the one thing the config keeps.
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
