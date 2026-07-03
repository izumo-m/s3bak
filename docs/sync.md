# Sync model: comparing, transferring, and the push / pull pipelines

This document covers how s3bak decides what to transfer, how it moves bytes, and
how the push and pull commands are assembled. See [manifest.md](manifest.md) for
the format the compare reads against, and [overview.md](overview.md) for the
module layout.

## The compare decision

Every sync needs a `compare=` strategy: given a pair (a local side and an S3
side for one key), does it need copying? s3bak has two.

### Default: `ManifestFilter` (stat quick check)

The default reads no file content. For a pair it copies unless the local file's
**size and mtime both match the manifest record** — an rsync-style quick check —
where mtime matches within `mtime_window` (default 2 s). A missing side, a key
the manifest does not know, or a stat that drifted all copy. It also copies when
the S3 side's size differs from the record, using the listing's size as free
evidence.

Why size + mtime, and why a window:

- **The manifest plays rsync's role of "the saved mtime".** Because the last
  push recorded the mtime, an unchanged file has a matching stat and is skipped
  without reading it.
- **The window absorbs filesystem mtime granularity.** The manifest stores
  nanosecond mtimes, but FAT rounds to 2 s, exFAT to 10 ms, NTFS to 100 ns. A
  pull that restores a nanosecond mtime onto a coarser filesystem would
  otherwise see a "difference" on the next run and re-download forever. The 2 s
  default covers all of them. `mtime_window = 0` restores exact-match behaviour.

**Accepted blind spot:** a change that leaves size *and* mtime equal to the
record — a content edit with the mtime restored (an mtime-preserving tool), or
an out-of-band S3 write at the same size — is invisible to the quick check. In a
multi-terminal workflow this bites the **push** side first: the terminal that
edited such a file never uploads it, so it never reaches S3 (its `status` is
quiet too, since `status` shares the predicate). `--checksum` (content) covers
it completely; a tighter `mtime_window` (e.g. `0`, exact) covers the case where
the mtime did advance but within the window. The window is set in `config.py`,
or overridden for one run with `--mtime-window <seconds>`.

**Self-healing (push):** a spurious mtime-only difference re-transfers the file
once; that push refreshes the manifest with the new mtime, and later runs pass
the quick check again. The converse does **not** hold for a *stale* manifest
(after `push --data-only`, or an out-of-band S3 write): `pull` never rewrites
the manifest, so affected pairs re-transfer on every pull until a real
`push` refreshes the record. This is deliberate — the manifest is the record of
the last real push, and only a push may change it.

### Opt-in: ETag content comparison (`--checksum`)

`--checksum` swaps in boto3-s3's `EtagComparison`, wrapped in `ParallelCompare`.
It copies a pair when the S3 ETag does not match the local file's reconstructed
ETag — so a same-size, same-mtime content change *is* transferred, and an
mtime-only drift is not. It reads and hashes every candidate file, which is why
it is opt-in. `part_size` comes from the same profile the uploads use, so
multipart ETags reconstruct to a matching value. `compare_workers` sets the
parallelism of this path (it is idle otherwise).

`status` and both compare directions share one size/mtime predicate
(`compare_to_local`), so `status` never disagrees with what a push or pull would
actually do. The window they use is `config.py`'s `mtime_window`, which
`--mtime-window <seconds>` overrides for a single run (0 = exact). `status` additionally reports mode changes for the metadata view —
but the sync never transfers over a mode change (that is a `--meta-only`
refresh, below).

## The transfer path: direct client call vs. `S3.cp`

boto3-s3's `S3.cp` always routes through s3transfer (a thread pool, and on
downloads a pre-transfer HeadObject probe). That machinery pays off for large,
multipart transfers but is overhead for a small object — and manifests, fetched
on nearly every command, are small.

So the store routes by size:

- **Below `min(multipart_threshold, s3.multipart_chunksize)`** (default 8 MiB):
  a direct `client.put_object` / `client.get_object`. One round trip; downloads
  skip the HeadObject probe (`status` and `ls-remote` drop from 2 S3 calls to
  1).
- **At or above the threshold:** `S3.cp`, for parallel multipart transfer and
  the composite multipart ETag.

**Why the min is a correctness gate, not just a tuning knob.** Below the
multipart threshold, s3transfer itself does a single-part upload, so a direct
`put_object` stores the *identical* plain-MD5 ETag `S3.cp` would. Above the part
size, `EtagComparison` reconstructs a *composite* ETag; a large file uploaded as
a single object would store a plain MD5 and break `--checksum`. Gating strictly
below the min of both means a small object is single-part under *both* the
transfer path and the ETag reconstruction, so a direct call can never diverge
from what `S3.cp` would store.

Downloads know a single-file entry's size from its manifest record and route
accordingly; a manifest download (small, size unknown) always takes the direct
path. Directory syncs (`sync_up` / `sync_down`) always use `S3.cp` — moving many
files is exactly what its machinery is for.

## Concurrency and the shared client

- `--all` runs entries through a thread pool, one thread per entry by default,
  capped at `entry_concurrency`. Each entry's own `cp` / `sync` then spawns
  s3transfer's transfer threads (`max_concurrency`), and `--checksum` its
  compare workers (`compare_workers`).
- boto3 client **construction** is not thread-safe. The store therefore builds
  one `S3` orchestrator and one boto3 client up front, in the single-threaded
  config-load path, and hands every S3-side location to the library as an
  `S3Storage` already bound to that client — so no client is ever constructed on
  a worker thread. A built client is safe to share; only construction races.

## The push pipeline

`cmd_push` for a whole entry:

1. Run `pre_hook` (always, before any work).
2. **Directory entry:** download the manifest (skipped under `--checksum`),
   build the compare strategy, and `sync_up` with `--delete` so removed local
   files are also removed on S3. Excludes are applied by the same entry-rooted
   matcher the manifest walk uses, so the data sync and the manifest can never
   disagree on what an exclude means.
   **Single-file entry:** upload iff the quick check fails — the manifest holds
   no matching-basename record, the local stat differs, or the S3 object is
   missing (a HeadObject confirms existence, since a single file has no listing
   to self-heal from). `--checksum` uses the ETag comparison instead.
3. **Refresh the manifest only if data was transferred** (or on `--meta-only`).
   A change the quick check cannot see — a mode/owner/group edit, or an mtime
   drift inside the window — transfers nothing and so does not refresh the
   manifest; `status` keeps showing it until a `--meta-only` push. This is
   deliberate.
4. Run `post_hook` — but only after a push that did work (transferred data
   and/or refreshed the manifest), so a side-effecting hook does not fire on a
   pure no-op. `--meta-only` always refreshes and runs the hook, which is the
   supported way to run the hook on demand.

A **sub-path push** (`push entry/sub` or a local path inside an entry) syncs or
uploads just that sub-tree and patches the manifest sub-tree in place
(`write_patched`). A symlink sub-path uploads no data — only its manifest record
is updated. If the entry has no manifest yet, the patch also writes the `.` root
record so the manifest keeps its directory-entry shape.

### Mode flags

- **`--meta-only`** refreshes the manifest (and runs `post_hook`) without
  transferring data. **Caution:** it asserts "S3 matches local" without making
  it true — a never-pushed local edit becomes invisible to the later quick
  check. It is a metadata refresh, never a substitute for a real push.
- **`--data-only`** transfers data but does not rewrite the manifest or apply
  local metadata.
- **`--dry-run`** reports what would happen and changes nothing; planned actions
  print with a `(dry-run)` marker.

## The pull pipeline

`cmd_pull`:

1. Download the manifest first — its records classify the entry as a directory
   or single file, and a sub-path as file / dir / symlink, with no extra
   head-object calls.
2. **Short-circuit:** if every manifest record already matches local
   (`_manifest_matches_local`), the sync and metadata apply are both no-ops, so
   pull returns immediately. This gate is skipped under `--checksum`, since it
   is the very stat check whose blind spot `--checksum` exists to cover.
3. **Download** (unless `--meta-only`, or a symlink sub-path with no data
   object): `sync_down` for a directory, a single `get_object` for a file
   (multipart via `S3.cp` if the recorded size is large). On Windows, read-only
   files the sync may overwrite are made writable first and restored after.
4. **Apply manifest metadata** (unless `--data-only`): recreate symlinks and
   empty directories, and set mode / mtime on everything. A regular file the
   manifest records but that no object placed is reported missing (exit 1),
   rather than silently created as a directory.

### Mode flags

- **`--meta-only`** applies recorded metadata without downloading data.
- **`--data-only`** downloads data without applying mode / mtime / symlinks.
- **`--delete`** removes local files not present in the manifest (a mirror
  restore).
- **`-o/--output`** restores to an alternative path instead of the entry's
  configured path.
