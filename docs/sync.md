# Sync model: comparing, transferring, and the push / pull pipelines

How s3bak decides what to transfer, how it moves bytes, and how the push and
pull pipelines are assembled. See [manifest.md](manifest.md) for the format the
compare reads against, [storage.md](storage.md) for the S3 object layout, and
[architecture.md](architecture.md) for module boundaries. What the commands do
from outside — options, output, prompts, exit codes — is the manual's
[command reference](manual/05-command-reference.md) and
[deleting safely](manual/06-deleting-safely.md); this document is the design
behind them.

## The compare decision

Every sync needs an **update-lane** strategy (`S3.sync`'s `update_filter`):
given a pair present on *both* sides (a local side and its S3 side for one key),
does it need re-copying? New entries and orphans are separate lanes —
`create_filter` copies every new local/S3 file, `delete_filter`
prunes orphans (off by default; `push --delete` turns it into the per-orphan
confirmation, `--yes` into an unconditional prune; pull prunes local extras
itself, see below) — so the strategy below only judges the
intersection. s3bak has two judgments; where each lives differs by direction:
pull wires `ManifestFilter` (or the `--checksum` comparison) as its update
filter directly, while push folds the same judgment into its journal emitter
(`PushJournal`), which spans all three lanes to record manifest changes as it
decides — see [journal.md](journal.md).

### Default: the size+mtime check

The default reads no file content. For a pair it copies unless the local file's
**size and mtime both match the manifest record** — an rsync-style size+mtime
check — where mtime matches within `mtime_window`. A missing side, a key the
manifest does not know, or a stat that drifted all copy. It also copies when
the S3 side's size differs from the record, using the listing's size as free
evidence.

Why size + mtime, and why a window:

- **The manifest plays rsync's role of "the saved mtime".** Because the last
  push recorded the mtime, an unchanged file has a matching stat and is skipped
  without reading it.
- **The window absorbs filesystem mtime granularity.** The manifest stores
  nanosecond mtimes, but a filesystem rounds them on restore, so a pull onto a
  coarser filesystem would otherwise see a "difference" on the next run and
  re-download forever. The window is a pure rounding tolerance, so among small
  values a larger one has the same real-world blind spot with wider safety.
  (Per-filesystem values are in the manual's
  [platform notes](manual/09-platform-notes.md).)

**Accepted blind spot:** a change that leaves size *and* mtime equal to the
record is invisible to the size+mtime check. `--checksum` covers it completely;
a tighter `mtime_window` covers only the case where the mtime did advance but
within the window. `verify --checksum` detects a file sitting in the blind spot
without uploading anything ([verify.md](verify.md)).

**Self-healing (push):** a spurious mtime-only difference re-transfers the file
once; that push refreshes the manifest with the new mtime, and later runs pass
the size+mtime check again. The converse does **not** hold for a *stale*
manifest (an out-of-band S3 write, or a push interrupted after its uploads):
`pull` never rewrites the manifest, so affected pairs re-transfer on every pull
until a real `push` refreshes the record. This is deliberate — the manifest is
the record of the last real push, and only a push may change it.

### Opt-in: ETag content comparison (`--checksum`)

`--checksum` swaps in boto3-s3's `EtagComparison`, run serially on the sync's
own thread — push's journal needs every lane decision in ascending key order,
so there is no compare pool. It copies a pair when the S3 ETag does not
match the local file's reconstructed ETag — so a same-size, same-mtime content
change *is* transferred, and an mtime-only drift is not. It reads and hashes
every candidate file, which is why it is opt-in. `part_size` comes from the same
profile the uploads use, so multipart ETags reconstruct to a matching value.

### One predicate, shared

`status` and both compare directions share one size/mtime predicate
(`compare_to_local` / `compare_to_stat`), so `status` never disagrees with what
a push or pull would actually do. The window is resolved per entry: the CLI
override beats a per-entry `mtime_window`, which beats the top-level one.
`status` additionally reports mode changes for the metadata view — the sync
never transfers over a mode change; a push refreshes just the manifest instead
(step 3 of the push pipeline, below), through the same mode predicate `status`
uses.

For a directory entry, `status` is one streaming merge-join
(`manifest.merge_join`) of the manifest against a fresh local walk, both in
S3 key order — every line in key order, holding one pair in memory, so a
manifest far larger than RAM still diffs in one pass. Each variant previews
its push: plain `status` previews a plain push, which touches nothing at a
manifest-only key, so it reports nothing there; `status --delete` previews
`push --delete`, whose candidates those keys are — locally deleted paths and
residue under excluded paths alike ([excludes.md](excludes.md)).

`status` never lists the bucket — its one S3 request is the manifest
download — so it previews only what the manifest records: an unrecorded
object, a stray object at the entry's own key, and the self-healing drop of a
stale record whose object is already gone are all invisible to it. The exact
rehearsal, with the real listing, is `push --delete --dry-run`; the passive
discovery channel for what status cannot see is `verify`.

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

A single-object transfer therefore reports its own result line, naming the
lane: a sync's lines come from boto3-s3's result callback, which knows nothing
of a transfer made outside it. The line prints after the transfer, like the
callback's own, so the stale-record branch (no object behind the record) warns
instead of announcing a download that never happened.

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
argument, never inserting a command shell: complex hook behaviour belongs in a
standalone executable that `config.py` names. Stdin is detached — entries push
concurrently and a `--delete` confirmation may be reading stdin on another
thread, so a hook that also read it would steal the answer.

A journal-driven `post_hook` run — the ordinary directory push (step 3 below)
and a sub-path push whose local target still exists — additionally gets
`S3BAK_JOURNAL` in its environment, naming the push journal file
([journal.md](journal.md)). The variable is absent from every other hook run —
a single-file entry's manifest write, a sub-path subtree deletion, and the
on-demand `hook` command — because none of those runs a journal-driven
compare; unset is defined as "no per-file detail available". Entries push
concurrently, so the variable is passed through the hook's own environment,
never through the process-wide one, and the file is deleted once the hook
returns. The contract as a hook author meets it is the manual's
[operating chapter](manual/07-operating.md); the on-demand
`s3bak hook pre|post` command, which runs the same contract outside any push,
is [cli.md](cli.md#hook-invocation-hook-prepost).

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
   records every manifest change. A locally deleted file keeps its S3 object
   AND its manifest record — **push never deletes a backup unless `--delete`
   was given and the deletion confirmed** (see "Deleting backups" below).
   `--checksum` ignores manifest file stats for its content decision (the
   download above still validates and feeds the kind check and the journal's
   cursor, which still journals mode and structure drift). Excludes filter the
   sync's **local side only**, per path, with aws-cli semantics
   ([excludes.md](excludes.md)) — through the same walker the manifest walk
   uses (`localwalk.sync_walker`), so the data sync and the manifest can never
   disagree on what an exclude means. The S3 listing is never filtered: an
   object under an excluded path — pushed before the exclude was added — is an
   ordinary delete-lane orphan, so `push --delete` can retire it instead of
   the exclude hiding it from every lane forever.
   **Single-file entry:** upload iff the size+mtime check fails — the manifest
   holds no matching-basename record, the local stat differs, or the S3 object
   is missing or size-drifted (a HeadObject confirms existence at the recorded
   size, since a single file has no listing to self-heal from). `--checksum`
   uses the ETag comparison instead.
3. **Publish the journal if it is non-empty — the only refresh condition.**
   `merge_journal` applies the journaled events to the old manifest and the
   result is validated and uploaded. Records with no event — the backups of
   locally vanished files included — survive verbatim; a `--delete` run
   journals the drops of confirmed deletions (object-owning and record-only
   alike) and of stale file records whose object was already gone (how an
   interrupted deletion self-heals). A record kept under a path that is no
   longer a directory (the local tree replaced a directory with a same-named
   file) makes the entry unrestorable as a tree; the merge detects this and
   warns (exit 2), and a `push --delete` prunes such records — the shadowed
   objects first, then their now-empty directory record. Journaling objectless
   changes makes empty-directory, symlink-only, and permission changes
   restorable even though they move no data: a `chmod` refreshes the manifest
   without re-uploading anything, settling exactly the mode differences
   `status` reports — the two share one mode predicate, which never compares
   symlink permission bits and on Windows reads only the owner-write bit
   (`os.stat` modes there are synthetic). A single-file entry runs the same
   permission check against its one record and rewrites its manifest from a
   fresh walk instead of a journal. An mtime drift inside the window transfers
   nothing and journals nothing — the window is a rounding tolerance. Owner
   and group are informational rather than comparison inputs, and refresh only
   when their record is rewritten for another reason. The walk warns (exit 2)
   when it cannot see the whole tree — an unreadable directory, a path racing
   away mid-walk — since the manifest is the record of what the push saw; the
   journal's records carry the very stats the compare judged, so that record
   is literal.
4. Run `post_hook` — but only after a push that did work (transferred data
   and/or refreshed the manifest), so a side-effecting hook does not fire on a
   pure no-op; `s3bak hook post <entry>` runs it on demand instead.

A push is not atomic: the data sync runs first and the manifest write last,
so a push interrupted between them leaves its new uploads as
[unrecorded objects](storage.md#unrecorded-objects). **Re-run the push after
any failure or interruption** — the re-run uploads whatever the manifest does
not know and writes the manifest that records it. An interrupted `--delete`
heals the same way ([recovery.md](recovery.md)).

A **sub-path push** (`push entry/sub` or a local path inside an entry) syncs or
uploads just that sub-tree and journals within its range: the journal stays
entry-rooted, events land only at/under `sub` (plus a missing or drifted
ancestor-directory record, so the ancestors' metadata restores — an excluded
ancestor stays unrecorded, which the manifest allows), and records outside
the range have no events, so the merge copies them verbatim. Like a
whole-entry push, it downloads and validates the manifest before any S3
mutation (the deletion, upload, and merge all reuse that one copy), and it
refuses a file-shaped entry — patching a sub-path into a single-file manifest
would corrupt it; push the entry itself to migrate the kind first. An
explicitly named file or symlink sub-path always re-records (naming the path
is the instruction to back up its current state; a file also always
re-uploads), while a directory sub-path follows the ordinary journal rules —
an unchanged sub-tree journals nothing and rewrites nothing. Excludes keep
their entry-root anchor inside the range (the sub walk re-anchors its keys),
and naming an excluded path does not override the exclude
([excludes.md](excludes.md)). If the entry has no manifest yet, the journal
also writes the `.` root record so the manifest keeps its directory-entry
shape. A file-typed sub-path has no S3 listing, so `--delete` there has
nothing to confirm: records under a same-named former directory are kept (with
the restorability warning), and pruning them takes a directory-level
`push --delete`.

If the local sub-path no longer exists (and is not excluded), the push fails
unless `--delete` is present — the guard that keeps a typo from silently
erasing a backup — and the deletion is confirmed as ONE question for the
whole subtree. Confirmed, s3bak deletes the exact data key and keys below
`<sub>/` (without touching a similarly prefixed sibling) and removes that
subtree from the manifest.

### Deleting backups (`--delete`, `--yes`)

Deleting is opt-in and confirmed per candidate. The prompt, its answers, and
what each outcome means to a user are the manual's
[deleting safely](manual/06-deleting-safely.md); the rules the implementation
is built on are these.

- **Keep by default, and keep the pair together.** `--delete` enables the
  delete lane behind a per-orphan confirmation; candidates arrive in ascending
  key order (the sync decides the delete lane serially). An object kept keeps
  its manifest record too — the record and the object always travel together,
  which is what keeps the manifest honest about what the backup holds. A
  question owns the terminal until it is answered, so a transfer result line
  can never scroll it away; prompts of parallel `--all` entries are serialized
  and carry the entry name.
- **A file record whose object is already gone needs no `--delete`.** It
  describes a backup that no longer exists, so retiring it is repair, not
  deletion: any push drops it silently. Deciding that requires seeing the S3
  side, which is why the delete lane is observed on every directory push even
  without `--delete` (it deletes nothing then); a sub-path push of a file or
  symlink lists no objects, so it proves nothing and keeps every record below
  it. This is the self-heal after an interrupted or `q`-aborted
  `push --delete`.
- **A record with no object is offered as the record itself.** A locally
  vanished symlink, special file, or directory left only its manifest
  record — and that record IS the backup — so `--delete` asks about it
  directly. Symlink and special-file records are asked as they arrive. A
  directory record is decided **post-order** — the ancestor-stack pattern
  `pull --delete` uses for local extras: asked only once every record beneath
  it resolved deleted, and kept silently the moment anything beneath
  survives, because dropping it would strip the recorded metadata from a
  directory the surviving records still restore into. A kept *unrecorded*
  object pins nothing: no record constrains the manifest. A vanished *empty*
  directory is the vacuous case, asked as soon as the stream moves past its
  key.
- **A candidate the manifest does not record** — an out-of-band upload, or
  the residue of a push interrupted before its manifest write — is flagged in
  the prompt. Keeping it cannot record it (a record describes a local file,
  and there is none), so the same question returns on every later `--delete`.
  See [storage.md](storage.md#unrecorded-objects).
- **An object under an excluded path is a candidate like any other**, because
  excludes filter only the local side of the sync
  ([excludes.md](excludes.md)). This is how the backup of a path excluded
  *after* it was pushed is cleaned up. A leftover excluded directory record
  follows the post-order rule above.
- **A kind-conflict object is offered out-of-lane.** A pushed file since
  replaced locally by a symlink or special file occupies its key in the
  complete-view walk, so the S3 object forms an update pair instead of an
  orphan and the sync's delete lane never sees it. `push --delete` collects
  such paired objects and offers each after the sync, through the same
  confirmation; a confirmed deletion removes just the object — the record
  already describes the local non-file and stays.
- **An incomplete local scan refuses deletions.** Once the walk warns about
  real tree content — an unopenable directory, a path that vanished
  mid-walk, an unreadable file the compare wanted to transfer — every later
  delete candidate is kept, object and record-only candidates alike, the
  journal writes no further drops, and the push warns (exit 2): an orphan
  decision built on a partial local view could delete a good backup. A sync
  that stops mid-stream engages the same gate: the manifest records it never
  reached are not evidence of deletion, so the emitter's final drain keeps
  them all without a question. Candidates already confirmed before the gap
  were decided on sound data and stand.
- **A single-file entry sweeps `entry/` explicitly.** Its push has no sync
  listing, so `--delete` lists the slash-bounded `entry/` prefix and offers
  every object there — always unrecorded, since a file-shaped manifest records
  only the entry's own key. This is how the residue of an entry that used to
  be a directory is retired; the manifest itself is untouched.
- **`--yes` is not a separate lane.** It auto-confirms the same candidates
  through the same paths, so an unattended run and its `--dry-run` rehearsal
  cannot diverge. Without a TTY on both stdin and stderr, `--delete` without
  `--yes` answers no to everything: a question nobody can see must not block,
  and keeping a backup is a valid answer rather than a failure.
- **`q` (abort)** stops the command the way any other failure does — the same
  "fail old" unwind as an S3 error or `Ctrl-C` ([recovery.md](recovery.md)):
  transfers not yet started are cancelled, deletions not yet sent are
  abandoned, the manifest is not rewritten, `post_hook` does not run. Nothing
  is published to describe the partial run, because the journal records each
  decision before its transfer completes, and a run that stopped cannot vouch
  for what it had already written. Two limits follow from stopping rather
  than finishing, and the next plain push settles both. **A request already in
  flight completes**: s3bak cancels what has not started, but never tears down
  a transfer mid-request, which would strand an abandoned multipart upload.
  **A confirmed deletion may or may not have run**: deletions batch (up to
  1,000 keys per request), so an object answered yes is gone if its batch had
  already been sent; flushing per answer would cost one request per object.

### `--dry-run`

`--dry-run` changes nothing, and only mutations, hooks, and prompts are
suppressed: every no-change step — listings, the manifest download and
validation, the compare decisions, the manifest merge (to a local temp file,
surfacing its warnings) — runs for real, making exactly the calls the real run
would make. Never a substitute call, whose permissions could differ: a
rehearsal must fail or warn exactly where the real command would. With
`--delete` it lists every candidate without prompting.

## The pull pipeline

`cmd_pull`:

1. Download the manifest first — its records classify the entry as a directory
   or single file, and a sub-path as file / dir / symlink, with no extra
   head-object calls.
2. **Short-circuit:** if every manifest record already matches local
   (`_manifest_matches_local`), the sync and metadata apply are both no-ops, so
   pull returns immediately. This gate is skipped under `--checksum`, since it
   is the very stat check whose blind spot `--checksum` exists to cover.
3. **Download** (a symlink sub-path, having no data object, skips this
   step): `sync_down` for a directory, a single `get_object` for a file
   (multipart via `S3.cp` if the recorded size is large). Excluded paths are
   not downloaded ([excludes.md](excludes.md)). A restore root of
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
4. **Apply manifest metadata**: one streaming merge-join of the manifest
   against a fresh local walk - the same join `status` and `--delete` run -
   repairs **only the records whose local state differs**: the shared
   size+mtime predicate plus mode, symlink target and own mtime (platform
   permitting), and directory mtime. A matching record is left untouched and
   unreported: an mtime drift inside the window is never "snapped" to the
   recorded value (the window is a rounding tolerance), and an unchanged
   symlink is not recreated. A downloaded file normally mismatches afterwards
   - the directory sync stamps the S3 object's upload time onto it, the
   single-file lane leaves the write time - and gets its recorded mtime
   applied; a stamp that already lands inside the window is a match and stays,
   the same bounded tolerance every match gets.

   Directory mode/mtime settles through an ancestor stack kept over the
   ascending merge-join: a directory pushes a frame when its own record is
   seen, and the frame is popped and settled - from a fresh lstat, re-checked
   against the record - as soon as the stream proves it has left that subtree,
   which is always after every child mutation (downloads ran before apply;
   symlink recreation and dir creation bump parent mtimes, and sort order puts
   them first). This keeps the whole apply streaming, with memory bounded by
   the depth of directories currently open rather than the tree size, and a
   directory dirtied only by the downloads themselves still converges in the
   same pull. A symlink replacing a local directory cannot settle inline — the
   lazy walk may not have descended into that subtree yet — so it is placed
   only after the whole stream is consumed; since that placement can dirty its
   own parent directory's mtime again, that parent's frame is flagged when the
   symlink is deferred and, instead of settling at pop time, is re-settled
   once more after deferred symlinks are placed.

   Excluded records are skipped and excluded local paths untouched
   ([excludes.md](excludes.md)); a restored child whose parent directory is
   excluded — and hence unrecorded — gets that directory created as a plain
   container, with no recorded metadata to apply. Mismatches repair in place:
   empty directories are recreated, symlinks are recreated with their recorded
   target and — where the platform can set it without following the link
   (`os.utime(..., follow_symlinks=False)`) — their own recorded mtime, and
   mode/mtime are set on entries whose local type matches the record.
   Directory and symlink conflicts are recreated from the manifest; a
   regular-file conflict is reported instead of following a hostile local
   symlink. A regular-file
   record whose object is gone — the residue of a deletion that outran its
   manifest rewrite, or an object removed out-of-band — is warned about and
   skipped in full (exit 2); the pull restores everything else rather than
   aborting a recovery over residue. Skipped in full means the local path is
   left exactly as it is, its metadata included: applying the record's
   mode/mtime over content the pull never restored would report a restore that
   did not happen, and hide a diverged local copy from every later size+mtime
   comparison.

What a pull can reproduce is bounded by what the backup records — see
[storage.md](storage.md#restore-fidelity).

### `pull --delete`

`pull --delete` removes local files the manifest does not record, behind the
same per-item confirmation as push. Candidates are the local-only lane of the
same manifest×walk merge-join `status` runs, streamed straight into the
removal - never materialized as a list. An excluded local path is invisible to
the extras diff and is never offered ([excludes.md](excludes.md)).

- **Order.** A leaf extra is judged the moment it arrives; a directory extra is
  pushed as an open frame on an ancestor stack and popped - and only then
  removed - once the stream proves it has left that subtree, so every removal
  inside a directory finishes before the `rmdir` that needs it gone. Memory
  stays bounded by the depth of directories currently open, not by how many
  extras exist. Keeping an item silently keeps every extra directory still
  open above it too — their `rmdir` could only fail — and is a choice, not a
  failure. A failed removal makes the command fail rather than report a
  successful mirror while an extra remains.
- **The name-folding alias set.** A local name that a name-folding filesystem
  (case-insensitive Windows/macOS, Win32's trailing dot/space trim, macOS
  NFC/NFD normalization) may fold onto a path the manifest records under a
  different spelling is excluded from removal, even though it matches no
  record byte-for-byte — it may be the very file the pull just restored under
  its recorded spelling. The alias set is collected in one preliminary pass
  over the same merge-join, before the removal stream starts, so a leaf and a
  directory extra alike check it the instant each is judged; a hit warns
  (exit 2) instead of removing.
- **Ordering against the metadata apply.** The extras pass runs after the
  apply and is skipped when the apply failed — extras diffed against a tree
  that is not in its recorded state are not trustworthy deletion candidates.
  Each removal bumps its parent directory's mtime, so when anything was
  removed the manifest metadata is applied once more, re-settling exactly the
  directories the removals dirtied.
- **Why it prompts per item.** An extra is judged solely by the manifest, so
  **an extra can be the only copy of real data**: a file never pushed, or one
  whose push was interrupted before its manifest write — its object then sits
  [unrecorded](storage.md#unrecorded-objects) on S3, or nowhere at all.
  Confirming its removal deletes local work the backup does not hold.

`--dry-run` reports the same decisions with the actions suppressed, with one
caveat: a conflicting-type restore root is only reported, and the rehearsal
sync runs against the uncorrected root, so its transfer report can differ from
what the real (staged) pull transfers.
