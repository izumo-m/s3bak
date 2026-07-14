# Manifest format (v3)

Each entry has one `<entry>-manifest.jsonl` object that records its filesystem
tree and metadata. It provides the input for `status`, metadata restoration on
`pull`, and the default sync comparison. See [storage.md](storage.md) for its
place alongside directly accessible data objects.

The format lives in `src/s3bak/manifest.py`, which is pure (stdlib only); the
tree walk that produces the records lives in `src/s3bak/localwalk.py`, on
boto3-s3's directory engine.

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
- **`owner` and `group` are informational.** They are shown by manifest-based
  reporting and refreshed when a push rewrites the manifest, but do not take
  part in comparison and are never applied with `chown`.
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
probe. JSON escaping keeps a filename containing a newline inside one record —
which a raw line-oriented format could not do. On POSIX, Python's
surrogate-escaped representation of non-UTF-8 filename bytes is also serialized
as JSON escapes rather than written as invalid UTF-8.

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

- **The walk** (`localwalk.walk_tree`) uses boto3-s3's complete, no-follow local
  enumeration — the data sync's own engine, so the sort definition cannot drift
  between the two. It returns directories, lstat-based symlink leaves, special
  files, and unreadable entries before filtering. s3bak customizes only exclude
  subtree pruning; boto3-s3 emits each directory record in-stream just before
  its children. An unreadable directory keeps its own record and silently loses
  its children.
- **The writer** streams walk → temp file → upload; it never buffers the whole
  manifest.
- **The status / pull `--delete` diff** (`merge_join`) pairs the manifest
  stream against a fresh walk on their shared sort keys with a one-record
  lookahead per side — both-sides pairs are compared (M), manifest-only
  records report D, local-only paths report A / become delete candidates — so
  a manifest far larger than RAM still diffs in one pass.
- **The sub-tree patch** (`write_patched`, used by a sub-path push) is a
  streaming merge of two already-sorted inputs — the old manifest and the newly
  walked sub-tree — instead of a read-all-then-sort. It copies old records
  outside the patched sub-path verbatim (preserving any unknown keys), drops
  old records at/under it, and splices the fresh records in at their sorted
  position.
- **The default update strategy** (`ManifestFilter`) reads the manifest once,
  front to back, merge-joining its records against `S3.sync`'s ascending
  compare-key pairs (`iter_compare_records` keys a directory as `name/` to match
  the pair order). It is wired as `S3.sync`'s `update_filter`, so it is handed
  only the both-sides pairs — new entries (`create_filter`) and orphans
  (`delete_filter`) are decided by those lanes, not here — and decides each with
  a one-record lookahead; the whole manifest is never loaded into a map. This is
  sound because a bare update filter is decided serially, in that same order (the
  one-record cursor self-heals over any key it is not asked about); only
  `--checksum`'s `ParallelFilter` content strategy runs in parallel, and it never
  wraps this filter. Because the filter holds the manifest file open for the
  whole sync, the caller `close()`s it before unlinking the temp manifest.

Because the order matches an S3 listing, the compare merge-joins the manifest
against either side of a sync without materializing it.

## Robustness

- Manifest reads fail closed. A bad header, invalid UTF-8, blank or malformed
  record, unsafe path, inconsistent type fields, duplicate, or out-of-order
  record aborts with `ManifestError` before manifest-driven mutation starts.
  Treating a damaged record as absent would be unsafe: `pull --delete` could
  otherwise classify the corresponding local path as an extra and remove it.
- Unknown record keys remain accepted and are preserved by sub-tree patches,
  so additive metadata does not require a version bump.

## Versioning and migration

The version bumps when the format changes incompatibly. There is no in-code
migration and no multi-version reader: to move to a new format, re-run
`push --all`, which regenerates every manifest. This is the same migration the
very first push performs — with no manifest on S3, every pair transfers and a
fresh manifest is written.

An old manifest object left behind after a filename change (e.g. a pre-v3
`<entry>-ls-l.txt`) is removed manually with the aws-cli command `aws s3 rm`;
s3bak keeps no code to clean up formats it no longer writes.
