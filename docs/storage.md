# Storage model

s3bak keeps regular-file contents directly accessible on S3 while recording
the filesystem state that S3 objects cannot represent in a separate manifest.
The configured entry name anchors each stored tree; absolute local parent paths
are not copied into S3 keys. The layout as a user meets it is the manual's
[introduction](manual/01-introduction.md); this document records why it is
shaped that way and which invariants the implementation must hold.

## Key layout

Everything for a configured `prefix = s3://<bucket>/<path-prefix>` lives under
that prefix. For a directory entry named `bin`:

```text
s3://bucket/backup/bin/...                 # data objects
s3://bucket/backup/bin-manifest.jsonl      # manifest object
```

A regular file whose entry-relative path is `sub/tool.py` is stored at
`.../bin/sub/tool.py`, its bytes unwrapped, unarchived, and uncompressed. A
single-file entry stores its data object at the entry key itself.

The entry name is one non-empty path component and cannot end in
`-manifest.jsonl`. The restriction prevents an entry's data key from colliding
with a manifest key.

## Data objects and filesystem objects

Only regular files have data objects. Directories, symbolic links, and special
files have none; their existence and state are represented by manifest records
alone. This preserves direct access to file contents without inventing
placeholder objects for filesystem-only concepts — standard S3 tooling can
list and retrieve file contents without s3bak, while reconstructing the
complete tree requires interpreting the manifest.

## The manifest

Each entry has one `<entry>-manifest.jsonl` object next to its data tree,
recording the entry-relative tree and the metadata comparison and restoration
need ([manifest.md](manifest.md) for the format).

The manifest represents the state of S3 as the last push recorded it: for
every data object, the metadata its restore needs; plus the records that ARE
the backup for the objectless kinds. Push is the manifest's only writer — pull
and read-only commands never rewrite it — and a push repairs whatever
divergence it can prove: a record whose object is gone, with nothing visible
locally at its path, is dropped by any push (repair, not deletion).

What a push cannot prove is left visible rather than guessed at: an unrecorded
object with no visible local source cannot be truthfully recorded (`verify`
reports it; `push --delete` retires it), and a content drift hiding behind an
unchanged size+mtime takes `verify --checksum` to detect and `push --checksum`
to repair. An out-of-band S3 change therefore leaves the manifest stale only
until the next push that can see it.

## Unrecorded objects

Push maintains a correspondence: every regular-file record has its data
object, and every data object has its record. The pair is created and kept
together, and a `--delete` answer covers both.

Retiring an object is a deletion, so it waits for `--delete` — the one seam in
the correspondence. When a regular file is replaced by a symlink or a special
file, a plain push records the new (objectless) kind but keeps the old data
object, which is now unreferenced: `verify` flags it as a type conflict, and
the next `push --delete` retires it. The mirror image needs no `--delete`: a
file record whose object is already gone backs nothing up, so any push retires
the record.

The correspondence has one gap s3bak cannot close: a data object the manifest
never recorded and whose local path shows the push nothing — uploaded
out-of-band with other S3 tooling, the fresh upload of a push interrupted
before its manifest write whose local file has since been deleted, or an
object under an excluded path, whose local side is invisible by rule
([excludes.md](excludes.md)). Such an object is outside the backup: `status`
cannot see it (it diffs the manifest against the local tree), and pull applies
no metadata to it, although pull's listing-driven download does fetch its bytes
(never for an excluded path).

A manifest record is a stat snapshot of a local file, so with no visible local
file there is nothing truthful to record: the object stays unrecorded and is
offered again on every later `--delete`. `verify` reports it passively, which
is the only channel that surfaces one without a `--delete` run
([verify.md](verify.md)).

When the unrecorded object's local counterpart exists and is visible, the gap
heals itself — the next push uploads and records the pair. The dangerous case
is a local file that was never recorded anywhere: `pull --delete` sees it as a
local extra and offers to remove it, and that confirmation plus a later
`push --delete`'s are all that stand between such a file and total loss, which
is why both prompt per item.

## Restore fidelity

A pull reproduces what the manifest and data objects represent — no more. The
limits (hard links, ownership, extended attributes, special files, torn files,
cross-platform names) are user-visible and belong to the manual: see
[what a backup preserves](manual/01-introduction.md#what-a-backup-preserves)
and [platform notes](manual/09-platform-notes.md).

Two of them constrain the implementation rather than merely describing it:

- **A filename must be valid UTF-8 to become an S3 key.** POSIX exposes
  undecodable filename bytes as surrogate code points; the manifest can
  round-trip them, but the S3 request encoding cannot, so pushing such a name
  fails that file's transfer. The push exits non-zero and the manifest is not
  rewritten — nothing corrupts.
- **A name-folding filesystem can split one path** between the manifest and a
  fresh local walk (case-insensitive Windows/macOS, Win32's trailing dot/space
  trim, macOS NFC/NFD). `pull --delete` must therefore treat a local name that
  folds onto a recorded one as not-an-extra, or it would remove the file the
  same pull just restored (see [sync.md](sync.md#the-pull-pipeline)).

See [manifest.md](manifest.md) for the exact record format and invariants, and
[sync.md](sync.md) for the commands that read or update it.
