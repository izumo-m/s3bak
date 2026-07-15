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

The manifest represents the state explicitly recorded by a push. Pull and
read-only commands never rewrite it. A normal push and `push --meta-only` may
refresh it; `push --data-only` deliberately leaves it unchanged. Consequently,
an out-of-band S3 change or a data-only push can leave the manifest stale until
a later push refreshes it.

## Unrecorded objects

Push maintains a correspondence: every regular-file record has its data
object, and every data object has its record. The pair is created, kept, and
deleted together — a `--delete` answer covers both, and a record whose object
is already gone (an interrupted deletion) is dropped by the next
`push --delete` merge. Directory, symlink, and special-file records stand
alone by design, as described above.

The correspondence has one gap s3bak cannot close: a data object the manifest
never recorded — uploaded out-of-band with other S3 tooling, or the fresh
upload of a push that was interrupted before it wrote the manifest, where the
local file has since been deleted. Such an object is outside the backup:
`status` cannot see it (it diffs the manifest against the local tree), and
pull applies no metadata to it, although pull's listing-driven download does
fetch its bytes. Its local counterpart, when one exists (a `--data-only` push
uploaded it), is invisible the same way: `pull --delete` sees a file the
manifest does not record — a local extra — and offers to remove it. Those two
confirmations, `pull --delete`'s and a later `push --delete`'s, are all that
stand between such a file and total loss, which is why both prompt per item
and why a `--data-only` push warns as it creates one.
`push --delete` offers its deletion like any other orphan,
flagging the prompt with `(not in manifest)`; answering n keeps the object
for this run only. A manifest record is a stat snapshot of a local file, and
with no local file there is nothing truthful to record, so the object stays
unrecorded and is asked about again on every later `--delete`. To adopt it
into the backup, materialize it locally (a pull downloads it) and push. To
retire it, answer y or run the `--yes` mirror. To keep it long-term without
adopting it, move it outside the entry prefix.

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
  paths within one entry).

See [manifest.md](manifest.md) for the exact record format and invariants, and
[sync.md](sync.md) for the commands that read or update it.
