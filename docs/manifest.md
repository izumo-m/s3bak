# Manifest format (v3)

Each entry has one manifest object on S3, `<entry>-manifest.jsonl`, next to its
data objects. The manifest records every path in the entry's tree with the
metadata S3 objects do not carry, and is the source of truth for `status`,
metadata restore on `pull`, and the default sync comparison.

The format lives in `src/s3bak/manifest.py`, which is pure (stdlib only).

## File layout

JSON Lines (one JSON object per line, UTF-8). The **first line is a header**;
every following line is one **entry record**, in S3-key order (see
[Ordering](#ordering)).

```jsonl
{"s3bak_manifest":3}
{"path":".","mode":"40755","owner":"me","group":"me","mtime_ns":1751600000000000000}
{"path":"./a.txt","mode":"100644","owner":"me","group":"me","size":5,"mtime_ns":1751600000000000000}
{"path":"./link","mode":"120777","owner":"me","group":"me","mtime_ns":1751600000000000000,"link":"a.txt"}
{"path":"./sub","mode":"40755","owner":"me","group":"me","mtime_ns":1751600000000000000}
{"path":"./sub/b.txt","mode":"100644","owner":"me","group":"me","size":4,"mtime_ns":1751600000000000000}
```

### Header

```json
{"s3bak_manifest": 3}
```

The single key carries the format version. A reader that does not recognise the
version raises `ManifestError` (exit 1) rather than mis-parsing — so an
incompatible manifest fails loudly. A missing or malformed header is the same
error.

### Entry record fields

| Field      | Type   | Present for            | Meaning                                             |
| ---------- | ------ | ---------------------- | --------------------------------------------------- |
| `path`     | string | every record          | entry-relative path (see below)                     |
| `mode`     | string | every record          | full `st_mode` as octal, e.g. `100644`, `40755`     |
| `owner`    | string | every record          | user name, or the uid as a string when unresolvable |
| `group`    | string | every record          | group name, or the gid as a string                  |
| `size`     | int    | regular files only    | byte size                                           |
| `mtime_ns` | int    | every record          | `st_mtime_ns` (nanoseconds since epoch)             |
| `link`     | string | symlinks only         | the symlink target, verbatim                        |

- **`mode` is the full `st_mode`**, type bits included, so the record alone
  distinguishes a regular file (`100…`), a directory (`40…`), and a symlink
  (`120…`). This is what lets `pull` restore an empty directory (no data
  object) or a symlink, and tell a recorded-but-never-uploaded file apart from
  a directory.
- **`path`** is the tree-walk path (see [`path`](#the-path-field)).
- Inapplicable keys are omitted: a directory or symlink has no `size`; only
  symlinks carry `link`. Readers ignore unknown keys, so a future field can be
  added without a version bump — the version changes only when an existing
  key's meaning changes.

### The `path` field

`path` is the entry-relative path, `/`-separated on every platform:

- `"."` — the entry root (a directory entry's top).
- `"./sub/file"` — a descendant, `./`-prefixed.
- `"solo.txt"` — a bare basename for a **single-file entry** (the whole entry
  is one file).

The `.` / `./` shape also tells `pull` whether the entry is a directory
(`.`-rooted) or a single file (one bare-basename record) without a head-object
probe. JSON encodes any byte sequence, so a filename containing a newline —
which the old line-oriented format could not represent — round-trips fine.

The value is named `rel` inside the tree walk (the mechanism that produces a
*relative* path); the manifest record stores that produced value in its `path`
field.

## Ordering

Records are written in **S3 key byte order** (aws-cli order), the same order S3
`ListObjectsV2` returns and boto3-s3's `LocalStorage.walk_local` emits. The key
rule: a directory's own record sorts immediately before its children, so a
directory keyed `foo/` comes *after* the sibling file `foo.txt` (because
`.` < `/`) and *before* `foo/bar`.

This single invariant is what keeps every manifest operation streaming, with
memory bounded by one directory level rather than the whole tree:

- **The walk** (`walk_tree`) sorts each directory level with real directories
  keyed as `name + "/"` and files/symlinks as `name`, then traverses
  depth-first — iteratively (an explicit stack), so tree depth is not bounded
  by Python's recursion limit. An unreadable directory is skipped silently (as
  `os.walk` was), since the data sync already surfaces it as a warning.
- **The writer** streams walk → temp file → upload; it never buffers the whole
  manifest.
- **The sub-tree patch** (`write_patched`, used by a sub-path push) is a
  streaming merge of two already-sorted inputs — the old manifest and the newly
  walked sub-tree — instead of a read-all-then-sort. It copies old records
  outside the patched sub-path verbatim (preserving any unknown keys), drops
  old records at/under it, and splices the fresh records in at their sorted
  position.

Because the order matches an S3 listing, a manifest can be merge-joined against
either side of a sync.

## Robustness

- A blank or damaged entry line is skipped, not fatal: a single corrupt line
  degrades that one record to "missing" rather than crashing `status`.
- A bad **header**, by contrast, aborts the read with `ManifestError` — the
  file is not a manifest this build understands, and silently reading it as
  empty would make `status` wrongly report "clean".

## Versioning and migration

The version bumps when the format changes incompatibly. There is no in-code
migration and no multi-version reader: to move to a new format, re-run
`push --all`, which regenerates every manifest. This is the same migration the
very first push performs — with no manifest on S3, every pair transfers and a
fresh manifest is written.

An old manifest object left behind after a filename change (e.g. a pre-v3
`<entry>-ls-l.txt`) is removed manually with `aws s3 rm`; s3bak keeps no code
to clean up formats it no longer writes.
