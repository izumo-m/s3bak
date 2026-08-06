# Push journal: the single-scan push

A directory push needs the local tree twice over: the compare decides what to
transfer, and the manifest rewrite records what the push saw. A multi-walk
pipeline would scan the tree separately for each; the push journal collapses
them into one scan: **the compare's single walk also emits every manifest
change it discovers, as a journal**, and the rewrite is a streaming
application of that journal to the old manifest, touching the filesystem no
further. The emitter is `syncops.PushJournal`; the format and merge live in
`manifest.py`.

Taking the record stats from the compare's own walk also closes a blind spot
a rebuild-walk pipeline has: a manifest rebuilt from a second walk after the
transfer records a file edited in between with its new stat while S3 holds
the old content — invisible to every later size+mtime check until
`--checksum`. The journal records exactly the stats the compare judged and
transferred by: an edit racing the push mismatches on the next run and
re-transfers — the ordinary self-healing.

## One scan, every lane

The sync's local side enumerates the **complete view** — directories,
symlinks as lstat leaves, special files — the same complete, no-follow
enumeration the manifest walk uses (`localwalk`), with the entry's excludes
filtered identically ([excludes.md](excludes.md)). boto3-s3's `Comparator` merge-joins that walk against the S3
listing into **one ascending stream of pairs**: both-sides (the update lane),
local-only (the create lane), S3-only (the delete lane).

- **One manifest cursor serves everything.** The old manifest is streamed
  once, front to back, advancing in lockstep with the pair stream. It backs
  the update lane's size+mtime comparison, the create lane's
  known-and-unchanged test, and absence detection: the update and create
  lanes together cover every local item, so a record the cursor skips over
  has no local counterpart — which is how a vanished objectless entry (a
  removed empty directory or symlink) is seen without a lane of its own.
- **Directories, symlinks, and special files have no S3 objects**, so they
  arrive local-only. The create lane journals their changes and never
  forwards them to the transfer engine.
- **The root `.` arrives first, in-stream.** The complete view emits the
  scanned root itself as the leading entry (compare key `""`, sorting before
  every child), so the root record needs no special casing: when its metadata
  drifted, its event is simply the journal's first line.
- **A kind conflict pairs instead of orphaning.** A local symlink or special
  file whose key holds a real S3 object (a pushed file since replaced by a
  symlink) forms an update pair, which shields the object from the delete
  lane. The emitter journals the type change (`!`) as usual; under `--delete`
  it offers the paired object as an out-of-lane candidate — confirmed like
  any other, deleted through s3bak's own store call, since the sync's delete
  lane never sees it. (Directory keys end in `/` and folder markers are
  hidden from the listing, so a directory forms no such pair.)
- **Readability is probed only on transfer.** The complete view enumerates a
  file whose metadata is readable but whose content is not, without the
  per-file open probe the transfer view would pay. The emitter probes just
  the files a lane decided to copy; an unreadable one is warn-skipped — no
  transfer, no journal event — keeping warn-and-continue (exit 2)
  semantics while probing only changed files instead of every file. An
  unchanged unreadable file passes the stat compare without being opened at
  all: a current backup is a clean no-op, not a warning.
- **Every complete-view walk warning is a real gap.** The view enumerates
  special files instead of warn-skipping them, so completeness tracking
  needs no special-device exemption: any warning — an
  unopenable directory, an entry that vanished mid-walk, a failed probe
  above — marks the scan incomplete and gates every `-` (see below). The one
  non-gap warning, the invalid-timestamp fallback, keeps its entry (the
  record is built from the raw `st_mtime_ns`, which the fallback does not
  touch) and is exempted.
- **Every decision is serial, in ascending key order** — the property the
  journal's ordering rests on. `--checksum` therefore runs its content
  comparison serially too; the parallel compare pool (`compare_workers`,
  `ParallelFilter`) is gone.

## Journal format

One line per event: **a one-character marker followed by a manifest v3 record
line** (the exact record format of [manifest.md](manifest.md)). No header, no
line numbers. The journal is a temporary file, never uploaded, but it is not
purely internal: a journal-driven push (the ordinary directory push and the
sub-path push) exposes the file to `post_hook` through the `S3BAK_JOURNAL`
environment variable (see [sync.md](sync.md#the-push-pipeline)) before
deleting it, which makes this format a hook-facing interface, not just an
implementation detail. s3bak deletes the file once the hook returns (or
immediately, on a push where no hook fires).

```
+{"path":"./docs/new.txt","mode":"100644","owner":"iz","group":"iz","size":12,"mtime_ns":1789000000000000001}
 {"path":"./old","mode":"40755","owner":"iz","group":"iz","mtime_ns":1700000000000000000}
-{"path":"./old/report.txt","mode":"100644","owner":"iz","group":"iz","size":900,"mtime_ns":1700000000000000000}
!{"path":"./src/main.py","mode":"100644","owner":"iz","group":"iz","size":2048,"mtime_ns":1789000000000000002}
```

| Marker      | Payload        | Meaning                                                    |
| ----------- | -------------- | ---------------------------------------------------------- |
| `+`         | the NEW record | the sort key is absent from the old manifest (an addition) |
| `!`         | the NEW record | the sort key is present (a replacement)                    |
| `-`         | the OLD record | drop the old record                                        |
| ` ` (space) | the OLD record | no change: keep the old record as-is                       |

- **The marker reflects the old manifest at the key, not the lane that
  produced the event.** A create-lane upload whose record already exists (a
  recorded file whose S3 object went missing) journals `!`; a file ↔ symlink
  type change shares one sort key and journals `!` too.
- An event's payload always carries the walked stat — the lstat the compare
  judged — with owner/group resolved at event time. A `-` / ` ` payload is
  the old record verbatim.
- A filename containing a newline stays inside one line (JSON escaping), so
  the format remains line-oriented.

### Invariants

- Lines are **strictly ascending by sort key, at most one line per key**. A
  `-` plus `+` at one key is an emitter bug (it must be `!`) and fails.
- The marker is cross-checked against the old manifest, fail closed: a `+`
  whose key exists, or a `!` / `-` / ` ` whose key does not, is a
  `ManifestError`. A `-` / ` ` payload must match the old record.
- Payloads pass the same record validation as manifest lines, and the merged
  output passes the full pre-publish validation every manifest write runs.

## What the emitter writes

- **No event**: an mtime drift inside the window (a rounding tolerance), and
  owner/group differences on an otherwise unchanged record. An untouched
  record keeps its old line verbatim — including its recorded mtime and
  owner/group, which refresh only when the record itself is rewritten.
- **`+` / `!`**: every transfer the update or create lane decides (including
  the S3-size-drift and missing-object re-uploads); a mode drift on a
  no-transfer pair, through the same mode predicate `status` uses; a symlink
  target change or (where the platform can set a symlink's own mtime without
  following it) an out-of-window symlink mtime drift; an out-of-window
  directory or special-file own-mtime drift; objectless additions (empty
  directory, symlink, special file); the root record when its metadata
  drifted.
- **`-`**: a stale old-only file record with no object behind it, dropped
  silently by **any** push — the record restores nothing, so retiring it is
  repair rather than deletion and needs neither `--delete` nor a question
  (this is why the delete lane is observed even without `--delete`: an
  object that is still there has to reach the emitter through that lane
  instead of arriving as a skip-over, and a run that lists no objects at all
  — a single-file or symlink sub-path push — proves nothing and keeps every
  record). The rest are `--delete` runs only: a confirmed object deletion
  (the object and its record travel together), and a
  confirmed record-only drop (a vanished symlink or special-file record,
  asked as its skip-over arrives; a vanished directory record, decided
  post-order through its ` ` line — next bullet). `--yes` is not a separate
  lane: it auto-confirms the same candidates through the same paths without
  prompting, so an unattended run and its `--dry-run` rehearsal cannot
  diverge. An orphan answered `n` writes nothing: the record stays. Once
  the walk warns about real tree content (`scan_incomplete`) — or the sync
  itself stopped mid-stream, parking the cursor over records it never
  reached — the emitter writes no further `-` — the partial-view gate that
  refuses deletions; drops journaled before the gap were decided on sound
  data and stand.
- **` ` (no change)**: the reserved line of a directory-record delete
  candidate. The decision is post-order — ask only once everything beneath
  the directory resolved deleted, keep it silently (no question) the moment
  anything beneath survives — the same ancestor-stack pattern `pull
  --delete` uses for local extras ([sync.md](sync.md#deleting-backups---delete---yes)).
  But the journal is strictly ascending, and a directory's line belongs
  before its children's, where the answer is not yet known. So the emitter
  writes the candidate as a no-change line the moment the cursor skips over
  it, remembers the offset (one open frame per ancestor directory, memory
  bounded by depth), and, when the drop is confirmed at the subtree's close,
  flips that line's marker byte to `-` in place — ` ` and `-` are the same
  one byte, so nothing shifts, and the journal is well-formed at every
  moment. A kept candidate simply remains a no-op line; no cleanup pass
  exists or is needed.

## The merge

The rewrite is a 2-way streaming merge of the old manifest against the
journal, both in ascending key order:

- a key with no event — the old record is copied **verbatim** (unknown keys
  preserved, no re-serialization);
- `+` / `!` — the payload is copied byte-for-byte, marker stripped;
- `-` — the old record is skipped;
- ` ` — the old record is copied verbatim, exactly like a key with no event
  (a kept delete candidate).

**The rewrite condition is "the journal holds at least one `+` / `!` /
`-`".** A first push journals `+` for everything including the root, so "no
manifest yet" needs no special case; a pure no-op push produces an empty
journal and rewrites nothing, and a `--delete` run whose directory-record
candidates were all kept leaves only ` ` lines — no real event, so nothing
is rewritten or re-uploaded (a cron all-no run must not republish an
identical manifest forever).

Every keep/drop policy — keep-by-default, the `--delete` confirmation, the
`--yes` auto-confirmation, the incomplete-scan gate — lives in the emitter as
"write a `-` or don't"; the merge applies events and knows no policy. (A
single-file entry's one-record write is the one non-journal manifest writer
left.)

## Scopes and mode flags

- **A sub-path push** applies the same scheme scoped to its range: the sub
  walk feeds the pair stream, the journal stays entry-rooted, and events land
  only at/under `sub` — plus a missing or drifted ancestor-directory record
  (excluded ancestors stay unrecorded), and the `.` root record when the push
  births the manifest, journaled as
  ordinary `+` / `!`. Records outside the range have no events and copy
  verbatim. An explicitly named, non-excluded file or symlink sub-path always
  re-records
  (naming the path is the instruction to back up its current state); a
  removed or excluded sub-path confirmed under `--delete` journals `-` for
  every record
  in the range. Skip-over drops apply only strictly *below* `sub`, and only
  when the sub-path push runs a sync at all: an object at exactly the `sub`
  key (a kind-changed former file) sits outside the slash-bounded sub
  listing, so its record is not provably stale and survives until a
  directory-level `push --delete` retires the pair — and a file or symlink
  sub-path, which lists nothing, proves nothing about any record below it.
- **A single-file entry** has no tree walk and no journal: its one-record
  manifest is rewritten from a fresh lstat.
- **`--dry-run`** runs the journal and the merge for real (to a local temp
  file, surfacing the same warnings a real push would) and skips only the S3
  upload — the same rehearsal contract as every no-change step.

## What this relies on in boto3-s3

No new boto3-s3 API is needed; the pipeline builds on existing behavior,
pinned by a contract test in boto3-s3's suite so s3bak can depend on it:

1. `enumerate_all_entries=True` on the sync source's `LocalStorage` flows
   into the sync scan unnarrowed (`walk_source_scan_options` documents the
   caller's duty to veto entries the transfer cannot consume — which the
   three lane filters do by returning `False`: nothing a filter rejects
   reaches the transfer engine).
2. The pair stream is one ascending merge-join of the two listings: the root
   leads at compare key `""`, directories are keyed with a trailing `/` (the
   manifest's own sort order), and every local entry carries its full lstat
   (`stat_result`), from which journal records are built.
3. Lane decisions run serially, in ascending key order, whenever no filter
   is wrapped in `ParallelFilter` — s3bak simply stops wrapping its
   `--checksum` comparison.
