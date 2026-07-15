# Sync model: comparing, transferring, and the push / pull pipelines

This document covers how s3bak decides what to transfer, how it moves bytes, and
how the push and pull commands are assembled. See [manifest.md](manifest.md) for
the format the compare reads against, [storage.md](storage.md) for the S3 object
layout, and [architecture.md](architecture.md) for module boundaries.

## The compare decision

Every sync needs an **update-lane** strategy (`S3.sync`'s `update_filter`):
given a pair present on *both* sides (a local side and its S3 side for one key),
does it need re-copying? New entries and orphans are separate lanes —
`create_filter` copies every new local/S3 file (the default), `delete_filter`
prunes orphans (off by default; `push --delete` turns it into the per-orphan
confirmation, `--yes` into an unconditional prune; pull prunes local extras
itself, see `--delete` below) — so the strategy below only judges the
intersection. s3bak has two.

### Default: `ManifestFilter` (size+mtime check)

The default reads no file content. For a pair it copies unless the local file's
**size and mtime both match the manifest record** — an rsync-style size+mtime check —
where mtime matches within `mtime_window` (default 10 ms, fractional seconds). A
missing side, a key the manifest does not know, or a stat that drifted all copy.
It also copies when the S3 side's size differs from the record, using the
listing's size as free evidence.

Why size + mtime, and why a window:

- **The manifest plays rsync's role of "the saved mtime".** Because the last
  push recorded the mtime, an unchanged file has a matching stat and is skipped
  without reading it.
- **The window absorbs filesystem mtime granularity.** The manifest stores
  nanosecond mtimes, but a filesystem rounds them on restore: NTFS to 100 ns,
  exFAT to 10 ms, FAT32 to 2 s. A pull that restores a nanosecond mtime onto a
  coarser filesystem would otherwise see a "difference" on the next run and
  re-download forever. The 10 ms default covers the common modern filesystems
  (NTFS, exFAT); a coarser one (FAT32 2 s, HFS+ 1 s) needs a larger value. The
  window is a pure rounding-tolerance, so among small values a larger one has
  the same real-world blind spot with wider safety. `mtime_window = 0` restores
  exact `st_mtime_ns` matching.

**Accepted blind spot:** a change that leaves size *and* mtime equal to the
record — a content edit with the mtime restored (an mtime-preserving tool), or
an out-of-band S3 write at the same size — is invisible to the size+mtime check. In a
multi-terminal workflow this bites the **push** side first: the terminal that
edited such a file never uploads it, so it never reaches S3 (its `status` is
quiet too, since `status` shares the predicate). `--checksum` (content) covers
it completely; a tighter `mtime_window` (e.g. `0`, exact) covers the case where
the mtime did advance but within the window. The window is set in `config.py`,
or overridden for one run with `--mtime-window <seconds>`.

**Self-healing (push):** a spurious mtime-only difference re-transfers the file
once; that push refreshes the manifest with the new mtime, and later runs pass
the size+mtime check again. The converse does **not** hold for a *stale* manifest
(after `push --data-only`, or an out-of-band S3 write): `pull` never rewrites
the manifest, so affected pairs re-transfer on every pull until a real
`push` refreshes the record. This is deliberate — the manifest is the record of
the last real push, and only a push may change it.

### Opt-in: ETag content comparison (`--checksum`)

`--checksum` swaps in boto3-s3's `EtagComparison`, wrapped in `ParallelFilter`
on a per-sync thread pool s3bak owns. It copies a pair when the S3 ETag does not
match the local file's reconstructed ETag — so a same-size, same-mtime content
change *is* transferred, and an mtime-only drift is not. It reads and hashes
every candidate file, which is why it is opt-in. `part_size` comes from the same
profile the uploads use, so multipart ETags reconstruct to a matching value.
`compare_workers` sizes that pool (it is idle otherwise).

`status` and both compare directions share one size/mtime predicate
(`compare_to_local` / `compare_to_stat`), so `status` never disagrees with what
a push or pull would actually do. The window is resolved per entry:
`--mtime-window <seconds>` (CLI, one run) overrides a per-entry `mtime_window`,
which overrides the top-level `mtime_window` in `config.py` (0 = exact
everywhere). A per-entry window suits a tree whose filesystem needs a different
tolerance than the rest. `status` additionally reports mode changes for the
metadata view — but the sync never transfers over a mode change (that is a
`--meta-only` refresh, below).

For a directory entry, `status` is one streaming merge-join
(`manifest.merge_join`) of the manifest against a fresh local walk, both in S3
key order: both-sides pairs run the shared predicate (M), manifest-only records
report D, local-only paths report A — every line in key order, holding one pair
in memory, so a manifest far larger than RAM still works. Excluded paths are
invisible on the local side of the diff: never compared, never an A — so a
record left in the manifest by a later-added exclude reads D until the next
push drops it.

## The transfer path: direct client call vs. `S3.cp`

boto3-s3's `S3.cp` always routes through s3transfer (a thread pool, and on
downloads a pre-transfer HeadObject probe). That machinery pays off for large,
multipart transfers but is overhead for a small object — and manifests, fetched
on nearly every command, are small.

So the store routes by size:

- **Below `min(multipart_threshold, s3.multipart_chunksize)`** (default 8 MiB):
  a direct `client.put_object` / `client.get_object`. One round trip; downloads
  skip the HeadObject probe (`status` and `ls-remote` drop from 2 S3 calls to
  1). A direct download streams into a sibling temporary file and atomically
  replaces the destination only after success, preserving an existing file if
  the response is interrupted and never following a final-path symlink.
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

## Concurrency

- Multi-entry commands run entries through a thread pool, one thread per entry
  by default, capped at `entry_concurrency`. This includes explicit target
  lists and `--all`. For push and pull, each entry's own `cp` / `sync` then
  spawns s3transfer's transfer threads (`max_concurrency`), and `--checksum`
  its compare workers (`compare_workers`).

All workers share the client constructed before the entry pool starts. See
[architecture.md](architecture.md#s3-client-lifetime) for its lifetime and
thread-safety boundary.

## The push pipeline

An entry's `pre_hook` and `post_hook` are non-empty argument lists. s3bak
executes the listed program directly and passes each remaining item as one
argument. It never inserts a command shell, so hooks do not perform shell
parsing, expansion, pipelines, or redirection. Complex hook behaviour belongs
in a standalone executable or script selected by `config.py` for the current
environment.

`cmd_push` for a whole entry:

1. Run `pre_hook` (always, before target validation or any backup work), so a
   hook may generate the file or tree being backed up.
2. **Directory entry:** download and validate the manifest, build the compare
   strategy, and `sync_up`. New and changed local files upload; a locally
   deleted file keeps its S3 object AND its manifest record — **push never
   deletes a backup unless `--delete` was given and the deletion confirmed**
   (see "Deleting backups" below). `--checksum` ignores manifest file stats
   for its content decision, but the manifest still detects objectless tree
   changes; only `--checksum --data-only` can skip the download. Excludes are
   applied by the same entry-rooted matcher the manifest walk uses, so the
   data sync and the manifest can never disagree on what an exclude means.
   **Single-file entry:** upload iff the size+mtime check fails — the manifest holds
   no matching-basename record, the local stat differs, or the S3 object is
   missing (a HeadObject confirms existence, since a single file has no listing
   to self-heal from). `--checksum` uses the ETag comparison instead.
3. **Refresh the manifest if data was transferred or deleted, its tree
   structure or a symlink target changed, on `--meta-only`, or when no
   manifest exists yet.** The refresh merges the fresh local walk into the
   old manifest (`write_merged`): a walked path always wins, and old-only
   records — the backups of locally vanished files — are kept, except file
   records dropped by a `--delete` run: those whose object the confirmation
   removed, and stale ones whose object was already gone (how an interrupted
   deletion self-heals). A record kept under a path that is no longer a
   directory (the local tree replaced a directory with a same-named file)
   makes the entry unrestorable as a tree; the merge detects this and warns
   (exit 2), and a `push --delete --yes` prunes such records. The
   structural check makes empty and symlink-only changes restorable even
   though they have no data objects. A mode-only change or an mtime drift inside
   the window transfers nothing and does not refresh an existing manifest;
   `status` keeps showing the mode or mtime difference until a `--meta-only`
   push. Owner and group are informational rather than comparison inputs, and
   update whenever the manifest is rewritten.
4. Run `post_hook` — but only after a push that did work (transferred data
   and/or refreshed the manifest), so a side-effecting hook does not fire on a
   pure no-op. `--meta-only` always refreshes and runs the hook, which is the
   supported way to run the hook on demand.

A **sub-path push** (`push entry/sub` or a local path inside an entry) syncs or
uploads just that sub-tree and patches the manifest sub-tree in place
(`write_merged` over the replaced range). A symlink sub-path uploads no data —
only its manifest record is updated. If the entry has no manifest yet, the
patch also writes the `.` root record so the manifest keeps its directory-entry
shape. A sub-path push is a whole-entry push scoped to the sub-path: the same
keep-by-default and `--delete` confirmation rules apply within the range, and
records outside it are copied verbatim. A file-typed sub-path has no S3
listing, so `--delete` there has nothing to confirm: records under a
same-named former directory are kept (with the restorability warning), and
pruning them takes a directory-level `push --delete`.

If the local sub-path no longer exists, the push fails unless `--delete` is
present — the guard that keeps a typo from silently erasing a backup — and the
deletion is confirmed as ONE question for the whole subtree. Confirmed, s3bak
deletes the exact data key and keys below `<sub>/` (without touching a
similarly prefixed sibling) and removes that subtree from the manifest.

### Deleting backups (`--delete`, `--yes`)

Deleting is opt-in and confirmed:

- **`--delete`** enables the delete lane behind a per-orphan prompt
  (`y/n/a/d/q`): y deletes this object, n keeps it, a deletes this and every
  later candidate, d keeps this and every later candidate, q aborts the whole
  command (a bare Enter re-asks; EOF aborts). Candidates arrive in ascending
  key order (the sync decides the delete lane serially). An object answered n
  keeps its manifest record too — the record and the object always travel
  together — and shows up as `D` in `status` until a later `--delete` removes
  it. Prompts of parallel `--all` entries are serialized and carry the entry
  name.
- **Only regular files are ever asked** — theirs are the S3 objects the
  delete lane sees. Directory, symlink, and special-file records have no
  object and no question, so no confirmation can drop them: they survive
  every `--delete` short of the `--yes` mirror. (For a locally deleted
  symlink or empty directory, the record IS the backup.)
- **`--yes`** answers yes to every confirmation: the unattended mirror for
  cron. Without a TTY (stdin/stderr), `--delete` without `--yes` answers no
  to everything — nothing is deleted and the run still succeeds (rc 0).
- **`q` (abort)** exits 1 without rewriting the manifest or running
  `post_hook`. Deletions already confirmed may have run; their records then
  linger until the next `push --delete`, whose merge drops any old-only file
  record that no longer has a delete candidate (the object is gone) and was
  not explicitly kept. The same self-healing covers a push interrupted
  mid-deletion.
- **`--meta-only` / `--data-only` cannot combine with `--delete`**: a deletion
  drops the object and its record atomically, which a one-sided push cannot.

### Mode flags

- **`--meta-only`** refreshes the manifest (and runs `post_hook`) without
  transferring data. The refresh is the same keep merge as an ordinary push,
  so records of locally vanished files survive it. **Caution:** it asserts
  "S3 matches local" without making it true — a never-pushed local edit
  becomes invisible to the later size+mtime check. It is a metadata refresh,
  never a substitute for a real push.
- **`--data-only`** transfers data but does not rewrite the manifest or apply
  local metadata.
- **`--dry-run`** reports what would happen and changes nothing; planned actions
  print with a `(dry-run)` marker. With `--delete` it lists every deletion
  candidate without prompting. Applies to pull too (see below).

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
   empty directories, and set mode / mtime on entries whose local filesystem
   type matches the record. Directory and symlink conflicts are recreated from
   the manifest; a regular-file conflict is reported instead of following a
   hostile local symlink. A regular file the manifest records but that no
   object placed is reported missing (exit 1), rather than silently created as
   a directory. Recorded owner and group names are not applied.

### Mode flags

- **`--meta-only`** applies recorded metadata without downloading data.
- **`--data-only`** downloads data without applying mode / mtime / symlinks.
- **`--delete`** removes local files not present in the manifest (a mirror
  restore), behind the same per-item confirmation as push: each extra is
  prompted `y/n/a/d/q` deepest-first (the removal order), `--yes` answers
  yes to everything, and a non-TTY run without `--yes` answers no (removes
  nothing, still exits 0). Keeping an item silently keeps its ancestor extra
  directories too — their `rmdir` could only fail — and is a choice, not a
  failure. Candidates are the local-only lane of the same manifest×walk
  merge-join `status` runs; only the extras themselves are held in memory, and
  they are removed deepest-first so directories empty out before their `rmdir`.
  A failed removal makes the command fail instead of reporting a successful
  mirror while an extra remains.
- **`--dry-run`** reports what a pull would do and changes nothing: planned
  downloads and `--delete` removals print with a `(dry-run)` marker, and a
  single `would apply manifest metadata` line stands in for the metadata
  apply (mode / mtime / symlinks) when it would run. The transfer report
  comes from the same sync decisions as a real pull, only with the actions
  suppressed.
- **`-o/--output`** restores one target to an alternative path instead of the
  entry's configured path. It is not available for multi-target pulls.
