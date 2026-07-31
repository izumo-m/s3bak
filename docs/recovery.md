# Interruption and recovery

What survives a command that did not finish, and what brings the backup back
into a consistent state. Two cases behave very differently:

- an **ordinary interruption** — an S3 error, a local I/O error, `Ctrl-C`, or
  a `q` answer to a `--delete` confirmation — unwinds through the normal
  paths, and the design answers it by falling back to the old state. `q` is
  deliberately not a special case: one unwind, one resulting state, whichever
  way a run stops ([sync.md](sync.md#deleting-backups---delete---yes));
- a **hard kill** — `kill -9`, a power loss — runs no handler and no
  `finally`. The backup itself stays consistent, but a few residues are left
  for the operator to resolve; they are listed at the end.

## The rule: fail old

Every S3 mutation completes before the manifest describing it is published,
and a manifest that drops records publishes only after the deletions those
records described have actually succeeded. A manifest is streamed to a local
temp file, validated with the reader's own rules, and only then uploaded as
one object — so a half-written manifest is never published, and the worst
surviving state is an arbitrary subset of completed uploads and deletions
described by the previous, still-valid manifest.

That shapes what converges an interrupted run:

- **additions and updates** converge with a plain `push` — the next scan sees
  the local files whose objects or records are missing and redoes them;
- **records whose object already went** — a deletion that completed on S3
  before the interruption — converge with a plain `push` as well: the record
  describes a backup that no longer exists, so any push retires it
  ([sync.md](sync.md#deleting-backups---delete---yes));
- **deletions that had not run yet, and record-only backups** — a vanished
  symlink, special file, or directory, whose record IS the backup — still
  need the `push --delete` that was interrupted. Those are real backups, and
  only a confirmed deletion may drop them.

`verify` is the read-only way to see where an interrupted run left the backup:
it reports missing data objects, unrecorded objects, and type conflicts
without changing anything (see [verify.md](verify.md)).

## Push

| Interrupted during | Surviving S3 state | Converged by |
| --- | --- | --- |
| the sync (uploads and deletions interleave) | any mix of completed uploads and deletions; the old manifest | plain `push` for the uploads and the now-stale records; `push --delete` for the deletions not yet run |
| between 1,000-key delete batches | earlier batches deleted, later ones intact; every record still in the old manifest | plain `push` drops the records whose objects are gone; `push --delete` also re-offers the rest |
| the out-of-lane deletions (kind conflicts, the entry's own key) | the ordinary sync finished; some out-of-lane objects deleted; the old manifest | `push --delete` |
| the journal merge or its validation | data objects in their new state; the old manifest | plain `push` re-derives the merge; `push --delete` if deletions were part of it |
| the manifest upload | either the old manifest or the complete new one — never a partial one | nothing, if the new one landed; otherwise as above |
| `post_hook` | the backup is fully consistent; only the hook's own effects are incomplete | **not automatic** — see below |

The explicit subtree deletion (`push --delete entry/gone-sub`), the entry-kind
migration, and the single-file stray sweep all follow the same order: the
record-dropping manifest publishes only after every delete batch succeeded.

## Pull

Pull never writes to S3, so an interruption can only leave the local tree
partly updated. Every record is re-judged from scratch on the next run, so a
plain `pull` converges the metadata and any missing data, and `pull --delete`
finishes the extras pass.

| Interrupted during | Surviving local state | Converged by |
| --- | --- | --- |
| the manifest download or validation | nothing touched — only a uniquely named temp file | plain `pull` |
| the data sync | fully written files only; a partial download is a uniquely named sibling temp, never the final name | plain `pull`; the temp shows up as an extra (`A`) inside the tree |
| the staged download, before the cutover | the old root is intact; only the stage holds partial data | plain `pull` |
| the metadata apply | some modes, mtimes, directories, and symlinks applied | plain `pull` — the apply only touches records whose local state differs |
| the extras removal | some extras removed, the rest still present; parent mtimes dirtied | plain `pull` re-settles the metadata; `pull --delete` removes the rest |

## What a hard kill does not guarantee

These need a manual step. None of them corrupts the backup on S3.

### A downloaded file may not be on disk yet

s3bak writes each downloaded file to a temp file and renames it into place,
which makes the replacement atomic **against a failing process** — but it does
not `fsync` the file or its parent directory, so it is not durable against a
power loss. On a write-back filesystem the rename and the applied mode/mtime
can survive while the file's contents do not. Because size and mtime then
still match the record, a later plain `pull` and `status` both call it a
match.

**After a power loss during a pull, re-run it with `--checksum`.** That
compares content rather than size and mtime, and re-downloads anything that
does not match. `verify` does not help here: it checks the manifest against
the stored objects, never against the local tree.

### An interrupted `post_hook` is not re-run

`post_hook` runs after the manifest is published, and s3bak keeps no durable
record that it ran. A kill during the hook therefore leaves the backup
complete and the hook's own effects — an off-site copy, a notification —
half-done, and the next plain `push` has nothing to do, so it does not run the
hook again.

**Re-run whatever the hook does by hand.** Note that `S3BAK_JOURNAL` describes
only the push that produced it, so it is not available afterwards.

### `*.s3bak-old-*` — a replaced directory

Restoring a symlink over a local directory cannot be atomic, so the directory
is renamed to an adjacent `<name>.s3bak-old-<random>`, the symlink is created,
and only then is the old directory removed. An ordinary failure rolls this
back; a hard kill in the middle leaves the target missing or the old subtree
sitting at the adjacent name.

**Re-run the pull** — it recreates the symlink. The leftover is not reclaimed
automatically: on a full-tree restore `status` reports it as an extra (`A`)
and `pull --delete` offers it, but a leaf sub-path restored with `-o` does not
walk its own siblings, so there it must be removed by hand.

### `*.s3bak-stage-*` — a staged restore root

When a pull must replace the restore root itself, it downloads into
`<root>.s3bak-stage-<random>/new` and swaps in two renames, moving the old root
to `<stage>/replaced` first. An ordinary failure or `Ctrl-C` preserves that
directory **and prints where it is**; a hard kill prints nothing, so the
configured path can be missing while the old tree sits in the stage.

**Re-run the pull** to restore the path, then inspect and remove the stage
directory yourself.

### Abandoned multipart uploads

A large upload is a multipart upload. An ordinary failure — including
`Ctrl-C` — aborts it, but a hard kill cannot, and s3bak neither lists nor
aborts leftover parts. They hold no valid object, are invisible to `verify`,
and are billed as storage.

**Configure the bucket with a lifecycle rule** carrying
`AbortIncompleteMultipartUpload`, which is the standard S3 answer; s3bak
keeps no code for it.

### Temp files

The temp manifests, journals, spools, and diff staging files a run creates are
unlinked in a `finally` that a hard kill skips, so they can accumulate in the
system temp directory. They never mislead a later run: every one is created
under a fresh unique name and none is read back, so removing them is only
housekeeping.
