# Sync model: comparing, transferring, and the push / pull pipelines

This document covers how s3bak decides what to transfer, how it moves bytes, and
how the push and pull commands are assembled. See [manifest.md](manifest.md) for
the format the compare reads against, [storage.md](storage.md) for the S3 object
layout, and [architecture.md](architecture.md) for module boundaries.

## The compare decision

Every sync needs an **update-lane** strategy (`S3.sync`'s `update_filter`):
given a pair present on *both* sides (a local side and its S3 side for one key),
does it need re-copying? New entries and orphans are separate lanes —
`create_filter` copies every new local/S3 file, `delete_filter`
prunes orphans (off by default; `push --delete` turns it into the per-orphan
confirmation, `--yes` into an unconditional prune; pull prunes local extras
itself, see `--delete` below) — so the strategy below only judges the
intersection. s3bak has two judgments; where each lives differs by direction:
pull wires `ManifestFilter` (or the `--checksum` comparison) as its update
filter directly, while push folds the same judgment into its journal emitter
(`PushJournal`), which spans all three lanes to record manifest changes as it
decides — see [journal.md](journal.md).

### Default: the size+mtime check

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
`verify --checksum` detects a file sitting in the blind spot without
uploading anything (see [verify.md](verify.md)).

**Self-healing (push):** a spurious mtime-only difference re-transfers the file
once; that push refreshes the manifest with the new mtime, and later runs pass
the size+mtime check again. The converse does **not** hold for a *stale* manifest
(after `push --data-only`, or an out-of-band S3 write): `pull` never rewrites
the manifest, so affected pairs re-transfer on every pull until a real
`push` refreshes the record. This is deliberate — the manifest is the record of
the last real push, and only a push may change it.

### Opt-in: ETag content comparison (`--checksum`)

`--checksum` swaps in boto3-s3's `EtagComparison`, run serially on the sync's
own thread — push's journal needs every lane decision in ascending key order,
so there is no compare pool. It copies a pair when the S3 ETag does not
match the local file's reconstructed ETag — so a same-size, same-mtime content
change *is* transferred, and an mtime-only drift is not. It reads and hashes
every candidate file, which is why it is opt-in. `part_size` comes from the same
profile the uploads use, so multipart ETags reconstruct to a matching value.

`status` and both compare directions share one size/mtime predicate
(`compare_to_local` / `compare_to_stat`), so `status` never disagrees with what
a push or pull would actually do. The window is resolved per entry:
`--mtime-window <seconds>` (CLI, one run) overrides a per-entry `mtime_window`,
which overrides the top-level `mtime_window` in `config.py` (0 = exact
everywhere). A per-entry window suits a tree whose filesystem needs a different
tolerance than the rest. `status` additionally reports mode changes for the
metadata view — the sync never transfers over a mode change; a push refreshes
just the manifest instead (step 3 of the push pipeline, below), through the
same mode predicate `status` uses.

For a directory entry, `status` is one streaming merge-join
(`manifest.merge_join`) of the manifest against a fresh local walk, both in S3
key order: both-sides pairs run the shared predicate (M), manifest-only records
report D, local-only paths report A — every line in key order, holding one pair
in memory, so a manifest far larger than RAM still works. Excluded paths are
invisible on the local side of the diff: never compared, never an A — so a
record left in the manifest by a later-added exclude reads D until a
`push --delete` retires it together with its object. The excluded
directory's own record is objectless; the same `push --delete` offers it
post-order, once everything beneath it is gone (see "Deleting backups").

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

- Multi-entry commands run entries through a thread pool, capped at 4 by
  default; `entry_concurrency` replaces that default (not a further cap on
  top of it) when set. This includes explicit target lists and `--all`. For
  push and pull, each entry's own `cp` / `sync` then spawns s3transfer's
  transfer threads (`max_concurrency`, ~10 by default) - so the default of 4
  concurrent entries already means around 40 transfers in flight, enough to
  saturate typical bandwidth; the cap also bounds how many clients and
  threads are built up front for entries that have not started yet. The
  compare itself is serial (push's journal needs ordered decisions).

Each entry worker slot gets its own S3 client, all constructed sequentially
before the entry pool starts (boto3-s3 forbids sharing a client across
concurrently transferring threads). See
[architecture.md](architecture.md#s3-client-lifetime) for the lifetime and
thread-safety boundary. `SIGINT` during a multi-entry run cancels the entries
that have not started; entries already running finish (killing one mid-push
would leave its manifest and data inconsistent) before the process exits 130.

## The push pipeline

An entry's `pre_hook` and `post_hook` are non-empty argument lists. s3bak
executes the listed program directly and passes each remaining item as one
argument. It never inserts a command shell, so hooks do not perform shell
parsing, expansion, pipelines, or redirection. Complex hook behaviour belongs
in a standalone executable or script selected by `config.py` for the current
environment.

A journal-driven `post_hook` run — the ordinary directory push (step 3 below)
and a sub-path push whose local target still exists — additionally gets
`S3BAK_JOURNAL` in its environment, naming the push journal file
([journal.md](journal.md)) so the hook can inspect exactly what the push
transferred and changed instead of assuming the worst. The variable is
absent from every other `post_hook` run — a `--meta-only` refresh, a
single-file entry's manifest write, and a sub-path push whose local target
had vanished (a subtree deletion or an ancestor-only `--meta-only` patch) —
because none of those runs a journal-driven compare. An unset `S3BAK_JOURNAL`
means "no per-file detail available; assume anything may have changed".
Entries push concurrently, so the variable is passed through the hook's own
environment, never through the process-wide one. The file is valid only
until the hook process exits — s3bak deletes it right after — so a hook that
needs the data later must copy it before returning. Under a directory push's
`--data-only`, `S3BAK_JOURNAL` is still set even though the manifest was not
rewritten: the journal reflects what the sync observed and transferred,
which is exactly what a `--data-only` hook needs to know.

`cmd_push` for a whole entry:

1. Run `pre_hook` (always, before target validation or any backup work), so a
   hook may generate the file or tree being backed up.
2. Download and validate the manifest — every push, of either entry kind,
   before anything on S3 moves, so a damaged manifest aborts while the backup
   is intact. An entry whose local path changed kind (file ↔ directory) is
   caught here: an ordinary push refuses — recording the new kind would
   silently orphan the old tree's objects, or plant an invalid bare-basename
   record inside a directory manifest — and `push --delete` migrates: one
   confirmed deletion removes the old backup (the exact key and everything
   under `entry/`), then the push records the new kind from scratch.
   **Directory entry:** one `sync_up` over the complete local view, with the
   journal emitter (`PushJournal`, [journal.md](journal.md)) wired as all
   three lane filters — the single scan that both decides the transfers and
   records every manifest change. New and
   changed local files upload; a locally
   deleted file keeps its S3 object AND its manifest record — **push never
   deletes a backup unless `--delete` was given and the deletion confirmed**
   (see "Deleting backups" below). `--checksum` ignores manifest file stats
   for its content decision (the download above still validates and feeds
   the kind check and the journal's cursor, which still journals mode and
   structure drift). Excludes prune the
   sync's **local side only**, through the same walker the manifest walk
   uses (`localwalk.sync_walker`), so the data sync and the manifest can
   never disagree on what an exclude means. The S3 listing is never
   filtered: an object under an excluded path — pushed before the exclude
   was added — is an ordinary delete-lane orphan, so `push --delete` can
   retire it (see "Deleting backups" below) instead of the exclude hiding
   it from every lane forever.
   **Single-file entry:** upload iff the size+mtime check fails — the manifest holds
   no matching-basename record, the local stat differs, or the S3 object is
   missing or size-drifted (a HeadObject confirms existence at the recorded
   size, since a single file has no listing to self-heal from). `--checksum`
   uses the ETag comparison instead.
3. **Publish the journal if it is non-empty — the only refresh condition.**
   A directory push's manifest changes were journaled during the sync (a
   transfer, a confirmed deletion, an objectless structural change, a symlink
   retarget or (platform permitting) own-mtime drift, a directory or
   special-file own-mtime drift, a permission drift, or the first push's
   `+`-everything journal);
   `merge_journal` applies them to the old manifest and the result is
   validated and uploaded. Records with no event — the backups of locally
   vanished files included — survive verbatim; a `--delete` run journals the
   drops of confirmed deletions (object-owning and record-only alike) and of
   stale file records whose object was already gone (how an interrupted
   deletion self-heals). A record kept under
   a path that is no longer a directory (the local tree replaced a directory
   with a same-named file) makes the entry unrestorable as a tree; the merge
   detects this and warns (exit 2), and a `push --delete` prunes such
   records — the shadowed objects first, then their now-empty directory
   record. Journaling objectless changes makes empty-directory,
   symlink-only, and permission
   changes restorable even though they move no data: a `chmod` refreshes the
   manifest without re-uploading anything, settling exactly the mode
   differences `status` reports — the two share one mode predicate, which
   never compares symlink permission bits and on Windows reads only the
   owner-write bit (`os.stat` modes there are synthetic). A single-file entry
   runs the same permission check against its one record and rewrites its
   manifest from a fresh walk instead of a journal. An mtime drift
   inside the window transfers nothing and journals nothing — the window is
   a rounding tolerance. Owner and group are informational rather than
   comparison inputs, and refresh only when their record is rewritten for
   another reason. The walk warns (exit 2) when it cannot
   see the whole tree — an unreadable directory, a path racing away
   mid-walk — since the manifest is the record of what the push saw; the
   journal's records carry the very stats the compare judged, so that record
   is literal.
4. Run `post_hook` — but only after a push that did work (transferred data
   and/or refreshed the manifest), so a side-effecting hook does not fire on a
   pure no-op. `--meta-only` always refreshes and runs the hook, which is the
   supported way to run the hook on demand. A directory entry's `post_hook`
   run is the journal-driven case described above (`S3BAK_JOURNAL` set); a
   single-file entry's and `--meta-only`'s are not (no compare, hence no
   journal).

A push is not atomic: the data sync runs first and the manifest write last,
so a push interrupted between them leaves its new uploads as
[unrecorded objects](storage.md#unrecorded-objects). **Re-run the push after
any failure or interruption** — the re-run uploads whatever the manifest does
not know (or, under `--checksum`, just detects the structural difference) and
writes the manifest that records it. An interrupted `--delete` heals the same
way (see "Deleting backups" below).

A **sub-path push** (`push entry/sub` or a local path inside an entry) syncs or
uploads just that sub-tree and journals within its range: the journal stays
entry-rooted, events land only at/under `sub` (plus a missing or drifted
ancestor-directory record — every record needs a recorded directory parent),
and records outside the range have no events, so the merge copies them
verbatim. Like a whole-entry push, it
downloads and validates the manifest before any S3 mutation (the deletion,
upload, and merge all reuse that one copy), and it refuses a file-shaped
entry — patching a sub-path into a single-file manifest would corrupt it;
push the entry itself to migrate the kind first. An explicitly named file or
symlink sub-path always re-records (naming the path is the instruction to
back up its current state; a file also always re-uploads), while a directory
sub-path follows the ordinary journal rules — an unchanged sub-tree journals
nothing and rewrites nothing. A symlink sub-path uploads
no data — only its manifest record is updated. Excludes keep their entry-rooted meaning
inside the range (the sub walk re-anchors them), with one deliberate edge: a
sub-path that is itself covered by an exclude is still walked, uploaded, and
recorded — naming a path explicitly wins over the exclude, and the data and
the manifest agree on the result. If the entry has no manifest yet, the
journal also writes the `.` root record so the manifest keeps its directory-entry
shape. A sub-path push is a whole-entry push scoped to the sub-path: the same
keep-by-default and `--delete` confirmation rules apply within the range. A
file-typed sub-path has no S3
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
  (`y/n/a/d/q/?`): y deletes this object, n keeps it, a deletes this and every
  later candidate, d keeps this and every later candidate, q aborts the whole
  command. Full words (yes/no/all/quit) are accepted; `?` or any answer not
  understood (a bare Enter included) prints the answer legend and re-asks,
  EOF aborts, and a one-line summary of the answers precedes the first
  question of a run. Candidates arrive in ascending
  key order (the sync decides the delete lane serially). An object answered n
  keeps its manifest record too — the record and the object always travel
  together — and shows up as `D` in `status` until a later `--delete` removes
  it. Prompts of parallel `--all` entries are serialized and carry the entry
  name.
- **A record with no object is offered as the record itself.** A locally
  vanished regular file is offered through its S3 object (above); a locally
  vanished symlink, special file, or directory left only its manifest
  record — and that record IS the backup — so `--delete` asks about it
  directly, the prompt flagged `(symlink record)` / `(special-file record)`
  / `(directory record)`, and a confirmed drop prints a `delete record:`
  line (there is no S3 object to print its own). Symlink and special-file
  records are asked as they arrive. A directory record is decided
  **post-order** — the ancestor-stack pattern `pull --delete` uses for local
  extras: it is asked only once every record beneath it resolved deleted
  (children before their own directory, in the same ascending key order as
  everything else), and the moment a record beneath survives — an object
  answered n keeps its record too — the directory record is kept silently,
  with no question of its own, because dropping it would strand the
  surviving records (every record needs its recorded directory parent). A
  kept *unrecorded* object pins nothing: no record constrains the manifest.
  A vanished *empty* directory is the vacuous case: nothing beneath, so its
  record is asked as soon as the stream moves past its key. The `a` / `d`
  answers stay sticky across candidate kinds, objects and records alike.
- **A candidate the manifest does not record** — an out-of-band upload, or
  the residue of a push interrupted before its manifest write — is flagged
  `(not in manifest)` in the prompt. Answering n keeps the object but cannot
  record it (a record describes a local file, and there is none), so the same
  question returns on every later `--delete`. See
  [storage.md](storage.md#unrecorded-objects) for what such an object is and
  how to adopt or retire it.
- **An object under an excluded path is a candidate like any other**, because
  excludes prune only the local side of the sync. This is how the backup of a
  path excluded *after* it was pushed (an accidentally uploaded `node_modules`,
  a log file excluded later) is cleaned up: `push --delete` offers the
  leftovers, and a confirmed deletion drops each object and its file record
  together. Answering n keeps both, and the local file — excluded, so
  invisible to the walk — stays untouched either way. The excluded
  directory's own record follows the rule above: a directory record, offered
  post-order once everything beneath it is retired.
- **A kind-conflict object is offered out-of-lane.** A pushed file since
  replaced locally by a symlink or special file occupies its key in the
  complete-view walk, so the S3 object forms an update pair instead of an
  orphan and the sync's delete lane never sees it. `push --delete` collects
  such paired objects and offers each after the sync, through the same
  confirmation (a/d stickiness carries over); a confirmed deletion removes
  just the object — the record already describes the local non-file and
  stays.
- **An incomplete local scan refuses deletions.** Once the walk warns about
  real tree content — an unopenable directory, a path that vanished
  mid-walk, an unreadable file the compare wanted to transfer — every later
  delete candidate is kept, object and record-only candidates alike, the
  journal writes no further drops (records travel with their objects), and
  the push
  warns (exit 2): an orphan decision built on a partial local view could
  delete a good backup. A sync that stops mid-stream (an error or an
  interrupt) engages the same gate: the manifest records it never reached
  are not evidence of deletion, so the emitter's final drain keeps them all
  without a question. Candidates already confirmed before the gap were
  decided on sound data and stand, their record drops already journaled;
  re-run `push --delete` after fixing the cause.
- **A single-file entry sweeps `entry/` explicitly.** Its push has no sync
  listing, so `--delete` lists the slash-bounded `entry/` prefix and offers
  every object there — always `(not in manifest)`, since a file-shaped
  manifest records only the entry's own key. This is how the residue of an
  entry that used to be a directory, or an out-of-band upload below a
  single-file entry, is retired; the manifest itself is untouched.
- **`--yes`** answers yes to every confirmation: the unattended mirror for
  cron. Without a TTY (stdin/stderr), `--delete` without `--yes` answers no
  to everything — nothing is deleted and the run still succeeds (rc 0).
- **`q` (abort)** exits 1 without rewriting the manifest or running
  `post_hook`. Deletions already confirmed may have run; their records then
  linger until the next `push --delete`, which journals the drop of any
  stale old-only file record with no object behind it. The same self-healing
  covers a push interrupted mid-deletion.
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
  local metadata. Uploading a file the manifest does not record therefore
  leaves an [unrecorded object](storage.md#unrecorded-objects) behind. A
  directory sync counts those uploads and warns (exit 2) — and since the
  default compare re-uploads a manifest-unknown key every run, the warning
  repeats on each later `--data-only` push until a push without
  `--data-only` records the file. A single-file entry or a file sub-path
  push uploads without this warning.
- **`--dry-run`** reports what would happen and changes nothing; planned actions
  print with a `(dry-run)` marker. Only mutations, hooks, and prompts are
  suppressed: every no-change step — listings, the manifest download and
  validation, the compare decisions, the manifest merge (to a local temp
  file, surfacing its warnings) — runs for real, making exactly the calls
  the real run would make (never a substitute call, whose permissions could
  differ), so a rehearsal fails or warns where the real command would. With
  `--delete` it lists every deletion candidate without prompting —
  record-only candidates as `(dry-run) delete record:` lines; with
  `--data-only` it previews the unrecorded-upload warning as "would upload".
  Applies to pull too (see below).

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
   (multipart via `S3.cp` if the recorded size is large). A restore root of
   the wrong type (a directory where a file entry restores, a file or symlink
   where a tree does) is never destroyed up front: the download lands in a
   unique stage directory beside it first, and the root is swapped in two
   atomic renames only after the download succeeded — a failed download costs
   nothing local, and the swap keeps the old root recoverable until the new
   one is in place. Before a directory
   sync into an existing tree, local symlinks sitting at recorded directory
   paths are replaced with real directories: the sync opens `dir/file` paths
   through whatever is at `dir`, and a symlink there would route downloads
   outside the restore tree (the root itself gets the same treatment). That
   replacement stands even if the download then fails — a conflicting
   symlink is never something a pull preserves. On
   Windows, read-only files the sync may overwrite are made writable first
   and restored after.
4. **Apply manifest metadata** (unless `--data-only`): one streaming
   merge-join of the manifest against a fresh local walk - the same join
   `status` and `--delete` run - repairs **only the records whose local state
   differs**: the shared size+mtime predicate plus mode, symlink target and
   own mtime (platform permitting), and directory mtime. A matching record is
   left untouched and unreported: an mtime drift inside the window is never
   "snapped" to the recorded value (the window is a rounding tolerance;
   `--meta-only --mtime-window 0` is the exact-value refresh), and an
   unchanged symlink (target and, platform permitting, mtime) is not
   recreated. A downloaded file normally mismatches afterwards - the
   directory sync stamps the S3 object's upload time onto it, the
   single-file lane leaves the write time - and gets its recorded mtime
   applied; a stamp that already lands inside the window is a match and
   stays, the same bounded tolerance every match gets. Directory mode/mtime
   settles through an ancestor stack kept over the ascending merge-join: a
   directory pushes a frame when its own record is seen, and the frame is
   popped and settled - from a fresh lstat, re-checked against the record -
   as soon as the stream proves it has left that subtree, which is always
   after every child mutation (downloads ran before apply; symlink recreation
   and dir creation bump parent mtimes, and sort order puts them first). This
   keeps the whole apply streaming, with memory bounded by the depth of
   directories currently open rather than the tree size, and a directory
   dirtied only by the downloads themselves still converges in the same pull.
   A symlink replacing a local directory cannot settle inline (see below) and
   is deferred until the whole stream is consumed; since placing it can dirty
   its own parent directory's mtime again, that parent's frame is flagged
   when the symlink is deferred and, instead of settling at pop time, is
   re-settled once more after deferred symlinks are placed. The walk prunes
   the entry's excludes (an excluded subtree is never scanned) but serves
   purely as a stat cache: a record it did not pair up is judged from a
   direct lstat, so a record under an excluded path - pull's data sync is
   exclude-blind - is still repaired. Mismatches repair as before: symlinks
   are recreated (restoring their own recorded mtime with
   `os.utime(..., follow_symlinks=False)` where the platform supports setting
   it without following the link) and empty directories are recreated, and
   mode / mtime set on entries whose local filesystem type matches the
   record. Directory and symlink conflicts are recreated from the manifest
   (a symlink replacing a local directory as the deferred placement above); a
   regular-file conflict is reported instead of following a hostile local
   symlink. A regular file the manifest records but that no object placed is
   reported missing (exit 1), rather than silently created as a directory.
   Recorded owner and group names are not applied.

What a pull can reproduce is bounded by what the backup records — see
[storage.md](storage.md#restore-fidelity) for the limits (hard links,
ownership and other attributes, special files, torn files, cross-platform
names).

### Mode flags

- **`--meta-only`** applies recorded metadata without downloading data. It
  runs the same gated apply, so it too repairs only the records whose local
  state differs.
- **`--data-only`** downloads data without applying mode / mtime / symlinks.
- **`--delete`** removes local files not present in the manifest (a mirror
  restore), behind the same per-item confirmation as push: each extra is
  prompted `y/n/a/d/q/?`, `--yes` answers yes to everything, and a non-TTY run
  without `--yes` answers no (removes nothing, still exits 0). Candidates are
  the local-only lane of the same manifest×walk merge-join `status` runs,
  streamed straight into the removal - never materialized as a list. A leaf
  extra is judged the moment it arrives, in the same order `status` would
  report it; a directory extra, by contrast, is not removed as it arrives but
  pushed as an open frame on an ancestor stack, and popped (and only then
  removed) once the stream proves it has left that subtree, so every removal
  inside a directory finishes before the `rmdir` that needs it gone. Memory
  stays bounded by the depth of directories currently open, not by how many
  extras exist. Confirmation and removal order is therefore subtree by
  subtree - children before their own directory - in the same ascending
  S3-key order as everything else, not one global deepest-first pass. Keeping
  an item silently keeps every extra directory still open above it too —
  their `rmdir` could only fail — and is a choice, not a failure. A local
  name that a name-folding filesystem (case-insensitive Windows/macOS,
  Win32's trailing dot/space trim, macOS NFC/NFD Unicode normalization) may
  fold onto a path the manifest records under a different spelling is
  excluded from removal the same way, even though it does not itself match
  any record byte-for-byte — it may be the very file the pull just restored
  under its recorded spelling. This alias set is collected in one
  preliminary pass over the same merge-join, before the removal stream even
  starts, so a leaf and a directory extra alike check it the instant each is
  judged - no deferral needed; a hit is reported as a warning (exit 2)
  instead of a delete line. A failed removal makes the command fail instead
  of reporting a successful mirror while an extra remains. The pass runs
  after the metadata apply and is skipped when that apply failed — extras
  diffed against a tree that is not in its recorded state are not
  trustworthy deletion candidates. Each removal bumps its parent directory's
  mtime, so when anything was removed
  the manifest metadata is applied once more, re-settling exactly the
  directories the removals dirtied. An extra is judged solely by the manifest,
  so **an extra can be the only copy of real data**: a file never pushed, or
  one pushed by `--data-only` or by a push interrupted before its manifest
  write — its object then sits [unrecorded](storage.md#unrecorded-objects)
  on S3, or nowhere at all. Confirming its removal (or running the unattended
  `--yes` mirror over it) deletes local work the backup does not hold; the
  per-item prompt exists for exactly this.
- **`--dry-run`** reports what a pull would do and changes nothing: planned
  downloads and `--delete` removals print with a `(dry-run)` marker, and a
  single `would apply manifest metadata` line stands in for the metadata
  apply (mode / mtime / symlinks) when it would run. The transfer report
  comes from the same sync decisions as a real pull, only with the actions
  suppressed — with one caveat: a conflicting-type restore root is only
  reported, and the rehearsal sync runs against the uncorrected root, so its
  transfer report can differ from what the real (staged) pull transfers.
- **`-o/--output`** restores one target to an alternative path instead of the
  entry's configured path. It is not available for multi-target pulls.
