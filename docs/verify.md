# Verification model (verify)

`verify` answers the question the other commands cannot: **does the backup
itself hold what the manifest promises, so that a pull would restore it?**
`status` compares the local tree against the manifest (what a push would do);
`diff` downloads content to compare it against local files. Neither ever
checks the manifest against the stored objects — until a restore fails,
nothing does. `verify` closes that gap, and it is strictly read-only: it needs
only `s3:ListBucket` and `s3:GetObject` (the manifest download), changes
nothing, and reports through the standard exit codes, which makes it safe to
run unattended from cron on any host — including one that has no local copy of
the tree.

```
s3bak verify [options] <entry|path>...
s3bak verify [options] --all
```

## The listing check (always on)

Per entry, verify downloads the manifest (strictly validated, as everywhere)
and streams one `ListObjectsV2` listing of the entry's data prefix. Both sides
ascend in S3 key byte order — the manifest's core ordering invariant — so a
single streaming merge-join checks the whole correspondence in one pass with
constant memory, and the listing already carries each object's size, ETag, and
storage class, so none of these checks costs an extra S3 call:

- **Missing data object** — a regular-file record with no object behind it. A
  pull of that file fails. Detects S3-side deletions, an interrupted
  `push --delete`, and lifecycle misconfiguration.
- **Size mismatch** — the object's size differs from the record. Restoring it
  would produce a file the push never saw: the residue of an out-of-band
  overwrite or a torn upload.
- **Type conflict** — a data object exists at a key the manifest records as a
  directory, symlink, or special file (only regular files have data objects).
  Typically the residue of a type change; the stray object collides with the
  restore of the recorded tree. The entry's own key is probed too, so a
  directory-shaped manifest with a leftover single-file object is caught.
- **Unrecorded object** — an object the manifest does not record (see
  [storage.md](storage.md#unrecorded-objects)). Until verify, these surfaced
  only inside a `push --delete` confirmation; verify lists them passively.
- **Folder object** — a `/`-terminated key, the manual-folder convention of
  the S3 console and some tools. s3bak never writes one. The zero-byte marker
  form is skipped by the data sync and is reported as noise to remove; one
  carrying data cannot restore to any local path and is an error.
- **Archived storage class** — an object in `GLACIER` or `DEEP_ARCHIVE`
  rejects `get_object` until manually restored, so a pull over it fails. This
  is checked for every listed object, recorded or not, since pull's
  listing-driven download fetches unrecorded objects too. (An
  `INTELLIGENT_TIERING` object in an archive tier fails the same way but is
  indistinguishable in a listing — a known limit.)
- **Missing backup** — a configured entry with no manifest. Data objects
  without any manifest (a push interrupted before its manifest write) are
  distinguished from an entry that was never pushed at all.

A single-file entry has no listing to stream; its one record is checked with
an exact head-object probe instead. A sub-path (`verify entry/sub`) scopes the
join to that subtree, like `status`.

## The content check (`--checksum`)

The listing check can only prove the manifest and S3 agree with each other —
both can agree and still be stale. On a machine that holds the local tree,
`--checksum` additionally compares every recorded file's local content against
the S3 ETag the listing already delivered (the same reconstruction as
`push --checksum`, same `compare_workers` pool, zero extra S3 calls). A
mismatch is split by the manifest stat, and the split is the point:

- **Content differs but size+mtime match** — an error. The default push skips
  this file forever: the size+mtime blind spot (an mtime-preserving edit, a
  same-size out-of-band overwrite, a torn upload of a since-settled file).
  Nothing else in s3bak reports this case before a restore does. The fix is
  `push --checksum`.
- **Pending change** — the stat drifted too, so this is an ordinary
  not-yet-pushed edit the next push picks up. Reported informationally,
  without affecting the exit code, so a routine edit does not page anyone.

A recorded file with no local counterpart (a kept deletion) or whose local
path changed type is skipped — the former is a normal backup state, the latter
is `status`'s finding. ETags that are not content MD5s (SSE-KMS) fail the
comparison loudly; the constraint is inherited from `--checksum` push.
`--mtime-window` tunes the classification stat and therefore requires
`--checksum` on this command.

## The top-level sweep (`--all`)

`verify --all` adds one non-recursive listing of the prefix top level and
warns about anything no configured entry accounts for: a stale manifest left
by a removed entry, a data tree with no manifest beside it, or a stray
top-level object. One command inventories the whole backup area.

## Severities and exit codes

Findings map onto the standard [exit codes](cli.md#exit-codes):

- **Errors (exit 1)** — the backup does not restore what the manifest
  promises: missing object, size mismatch, type conflict, data-carrying
  folder object, archived storage class, silent content divergence, missing
  backup, damaged manifest.
- **Warnings (exit 2)** — the recorded backup restores, but something sits
  outside it: unrecorded objects, zero-byte folder objects, everything the
  `--all` sweep reports.
- **Informational** — pending changes (`--checksum`); no exit-code effect.

Every entry also prints a one-line summary (`OK` or finding counts, with
record and object tallies), so a quiet cron log still shows the check ran.

## What verify does not do

- **No local metadata comparison.** S3's `LastModified` is the upload time,
  not the file's mtime; local-vs-manifest drift is `status`.
- **No repair.** Remediation is `push` (missing/changed data),
  `push --checksum` (silent divergence), `push --delete` (unrecorded
  objects), or `aws s3 rm` (folder objects, stale manifests).
- **No restore drill.** verify proves the pieces are present and intact, not
  that a restore workflow works end to end. Periodically pull into a scratch
  directory (`pull <entry> -o /tmp/drill`) and inspect the result; back up
  live databases via a `pre_hook` dump, as
  [storage.md](storage.md#restore-fidelity) describes.

## Suggested routine

- `s3bak verify --all` daily — listing-only, one manifest GET plus one LIST
  page per ~1000 objects per entry.
- `s3bak verify --all --checksum` weekly — reads and hashes every recorded
  local file.
- A restore drill quarterly, or after changing excludes, entries, or bucket
  policy.

Under a credential-separation policy, the verifying host's credentials need no
write or delete permissions at all.
