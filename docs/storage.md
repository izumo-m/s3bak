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

See [manifest.md](manifest.md) for the exact record format and invariants, and
[sync.md](sync.md) for the commands that read or update it.
