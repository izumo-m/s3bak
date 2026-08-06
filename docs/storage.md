# Storage model

s3bak keeps regular-file contents directly accessible on S3 while recording
the filesystem state that S3 objects cannot represent in a separate manifest.
The configured entry name anchors each stored tree; absolute local parent paths
are not copied into S3 keys.

## Key layout

Everything for a configured `prefix = s3://<bucket>/<path-prefix>` lives under
that prefix. For a directory entry named `bin`:

```text
s3://bucket/backup/bin/...                 # data objects
s3://bucket/backup/bin-manifest.jsonl      # manifest object
```

For entry `bin`, a regular file whose entry-relative path is `sub/tool.py` is
stored at `.../bin/sub/tool.py`. Its bytes are not wrapped, archived,
compressed, or otherwise transformed by s3bak. A single-file entry stores its
data object at the entry key itself, such as `.../wsl.conf`.

The entry name is one non-empty path component and cannot end in
`-manifest.jsonl`. The restriction prevents an entry's data key from colliding
with a manifest key.

## Data objects and filesystem objects

Only regular files have data objects. Directories, symbolic links, and empty
directories have no corresponding data object; their existence and state are
represented by manifest records. This preserves direct access to file contents
without inventing placeholder objects for filesystem-only concepts.

Standard S3 tooling can therefore list and retrieve regular-file contents
without s3bak. Reconstructing the complete filesystem tree, including metadata
and objectless entries, requires interpreting the manifest.

## The manifest

Each entry has one `<entry>-manifest.jsonl` object next to its data tree. It
records the entry-relative tree and the metadata used for comparison and
restoration, including filesystem type, mode, size, mtime, symbolic-link
target, owner, and group. Owner and group are retained for reporting only;
s3bak does not change ownership during restore.

The manifest represents the state of S3 as the last push recorded it: for
every data object, the metadata its restore needs; plus the records that ARE
the backup for the objectless kinds (directories, symlinks, special files).
Push is the manifest's only writer — pull and read-only commands never
rewrite it — and a push repairs whatever divergence it can prove: a record
whose object is gone, with nothing visible locally at its path, is dropped by
any push (repair, not deletion). What a push cannot prove is left visible
rather than guessed at: an unrecorded object with no visible local source
cannot be truthfully recorded (`verify` reports it; `push --delete` retires
it), and a content drift hiding behind an unchanged size+mtime takes
`verify --checksum` to detect and `push --checksum` to repair. An
out-of-band S3 change therefore leaves the manifest stale only until the
next push that can see it.

## Unrecorded objects

Push maintains a correspondence: every regular-file record has its data
object, and every data object has its record. The pair is created and kept
together, and a `--delete` answer covers both.

Retiring an object is a deletion, so it waits for `--delete` — this is the one
seam in the correspondence. When a regular file is replaced by a symlink or a
special file, a plain push records the new (objectless) kind but keeps the old
data object, which is now unreferenced: `verify` flags it as a type conflict,
and the next `push --delete` retires it. The mirror image needs no `--delete`:
a file record whose object is already gone (an interrupted deletion) backs
nothing up, so any push retires the record — repair, not deletion.
Directory, symlink, and special-file records stand alone by design, as described
above.

The correspondence has one gap s3bak cannot close: a data object the manifest
never recorded and whose local path shows the push nothing — uploaded
out-of-band with other S3 tooling, the fresh upload of a push that was
interrupted before it wrote the manifest and whose local file has since been
deleted, or an object under an excluded path, whose local side is invisible
by rule ([excludes.md](excludes.md)). Such an object is outside the backup:
`status` cannot see it (it diffs the manifest against the local tree), and
pull applies no metadata to it, although pull's listing-driven download does
fetch its bytes (never for an excluded path). When the unrecorded object's
local counterpart exists and is visible, the gap heals itself — the next push
uploads and records the pair; the dangerous case is a local file that was
never recorded anywhere: `pull --delete` sees a file the manifest does not
record — a local extra — and offers to remove it, and that confirmation plus
a later `push --delete`'s are all that stand between such a file and total
loss, which is why both prompt per item.
`push --delete` offers an unrecorded object's deletion like any other orphan,
flagging the prompt with `(not in manifest)`; answering n keeps the object
for this run only. A manifest record is a stat snapshot of a local file, and
with no visible local file there is nothing truthful to record, so the object
stays unrecorded and is asked about again on every later `--delete`. To adopt
it into the backup, materialize it locally (a pull downloads it) and push —
an excluded one needs its exclude lifted first. To
retire it, answer y or run the `--yes` mirror. To keep it long-term without
adopting it, move it outside the entry prefix. `verify` reports every
unrecorded object passively — no `--delete` run required (see
[verify.md](verify.md)).

## Restore fidelity

A pull reproduces what the manifest and data objects represent — no more. The
known limits, as input for choosing what to back up:

- **Hard links are not detected.** Each linked path is walked, uploaded, and
  restored as an independent regular file; the link relationship is lost.
- **Mode and mtime are the only attributes restored.** Owner and group are
  recorded for reporting but never applied — a root-owned file such as
  `/etc/wsl.conf` comes back owned by whoever ran the pull. Extended
  attributes, ACLs, and file sparseness are not recorded at all.
- **Special files are not recreated.** A device node, FIFO, or socket is
  recorded (type, mode, mtime), and a pull applies mode and mtime where one
  already exists with the recorded type — but a missing one is reported as an
  error (exit 1), never created.
- **A file written during a push can upload torn.** s3bak takes no filesystem
  snapshot, so a live database file may be captured mid-write and restore
  corrupt. Dump such files with a `pre_hook` and back up the dump.
- **A tree may not restore across platforms.** A filename that is legal where
  it was pushed can be impossible where it is pulled: Windows rejects
  characters such as `:` and reserved names such as `aux`, and creating a
  recorded symlink there may require Developer Mode or elevation — the pull
  fails on such records. On a case-insensitive filesystem, two recorded paths
  that differ only by case land in one local file, the last download winning
  silently (the destination-overlap check guards distinct pull targets, not
  paths within one entry). The same folding can also split ONE path between
  the manifest and the local walk instead: a name-folding filesystem (case-
  insensitive Windows/macOS, Win32's trailing dot/space trim, macOS NFC/NFD
  Unicode normalization) can store a file under a different byte spelling
  than the manifest recorded it under, so a fresh local walk reports it at
  that other spelling. `pull --delete` recognizes this - the local name folds
  onto the recorded one - and excludes it from removal with a warning rather
  than treating it as an extra it just restored.
- **A filename must be valid UTF-8 to become an S3 key.** POSIX exposes
  undecodable filename bytes as surrogate code points; the manifest can
  round-trip them, but the S3 request encoding cannot, so pushing such a name
  fails that file's transfer (the push exits non-zero and the manifest is not
  rewritten — nothing corrupts, but the file cannot be backed up until it is
  renamed or excluded).

See [manifest.md](manifest.md) for the exact record format and invariants, and
[sync.md](sync.md) for the commands that read or update it.
