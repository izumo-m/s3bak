# How s3bak detects changes

s3bak decides what has changed by comparing each path's **size and
modification time** against what the last push recorded. It never reads a
file's contents to find out. That is what makes a routine push cheap enough to
run often, and it is also the source of the one change s3bak can miss.

This chapter is that decision in full: the rule, what counts as equal, how to
read the result, where the rule fails and what covers it, what the other
commands compare instead, and what `excludes` takes out of the picture
entirely.

## The rule: size and modification time

What the last push recorded is the **manifest** — one line per path, holding
that path's type, permission bits, size, modification time and, for a symlink,
its target. It sits in the bucket beside the data objects, and every command
that compares anything downloads it first.

A path that both the manifest and the local tree know about counts as
**unchanged when its size and its modification time both match the record**,
the modification time within a tolerance described below. Nothing else is
consulted, and the file is never opened.

What gets compared depends on what the record says the path is:

| Recorded as | Compared against the local path |
| --- | --- |
| regular file | size, modification time, permission bits |
| directory | modification time, permission bits — never size |
| symlink | its target, and its own modification time where the platform can set one; permission bits never |
| special file | modification time, permission bits |

Permission bits are compared but never move data. A `chmod` alone makes the
next push rewrite the manifest record and upload nothing.

Here is a tree with a few unrelated changes — a longer file, a new file, and
a `chmod`. Each `M` line names the properties that differed:

```console
$ s3bak status demo
M /home/you/demo/lib	mtime
A /home/you/demo/lib/new.sh
M /home/you/demo/notes.txt	size, mtime
M /home/you/demo/run.sh	mode
```

`-v` prints the values behind those tags, along with a trace of the S3
requests:

```console
$ s3bak status -v demo
+ (boto3) get_object s3://my-bucket/backup/demo-manifest.jsonl
M /home/you/demo/lib	mtime
      mtime: remote=2026-08-02 13:42:56 < local=2026-08-02 13:43:07 (+11s)
A /home/you/demo/lib/new.sh
M /home/you/demo/notes.txt	size, mtime
      size: remote=23 < local=34 (+11 bytes)
      mtime: remote=2026-08-02 13:42:56 < local=2026-08-02 13:43:07 (+11s)
M /home/you/demo/run.sh	mode
      mode: remote=755 local=700
```

`lib` is listed because adding a file to a directory changes that directory's
own modification time, which s3bak records and restores like any other.

## Reading status

The letters describe what a push would do to the backup, not what happened
locally. `M` is a path both sides know about whose record no longer matches,
`A` exists only locally. Silence means everything matched — including
everything an ordinary push would leave alone: the record of a locally
deleted file is kept, so plain `status` says nothing about it.

`status --delete` previews `push --delete` instead, and that is where `D`
appears: a path that exists only in the backup — a locally deleted file, or
residue under a later-added exclude — that `push --delete` would offer to
remove:

```console
$ mv /home/you/demo/old-notes.txt /tmp/
$ s3bak status --delete demo
M /home/you/demo	mtime
D /home/you/demo/old-notes.txt
```

Plain `status` shows only the `M` line: an ordinary push updates the
directory's record and keeps the backup of `old-notes.txt`, exactly as the
absent `D` suggests. `status` never lists the bucket, so the preview covers
what the manifest records; an object the manifest does not know about is
`verify`'s to report, and the exact rehearsal — with the real listing and
the real confirmations — is `push --delete --dry-run`.

A path whose *type* changed is a modification: a push re-records the new
kind, so the pair prints as `M` with a `type` tag. A regular file replaced
by a symlink stays one line, because the two occupy the same key:

```console
$ s3bak status demo
M /home/you/demo/lib	mtime
M /home/you/demo/lib/util.sh	type
```

A regular file replaced by a **directory** sorts to a different key, so
nothing pairs them up: the new directory prints `A`, and the old file's
record — kept by an ordinary push — surfaces as `D` only under
`status --delete`:

```console
$ s3bak status --delete demo
M /home/you/demo	mtime
D /home/you/demo/run.sh
A /home/you/demo/run.sh
```

## When two modification times count as equal

Modification times are compared with a tolerance, `mtime_window`, defaulting
to 10 milliseconds. A difference **up to and including** the window is a
match; anything larger is a difference.

The tolerance exists because filesystems disagree about how finely they store
a modification time. The manifest holds nanoseconds, but restoring that onto a
filesystem that rounds to 10 ms or 2 s produces a file whose modification time
is no longer the recorded one — and without a tolerance, the next run would
see a difference and download it again, forever.

The window is therefore a rounding allowance, not a grace period. Among small
values a larger one has the same practical blind spot with more safety, so
matching it to the filesystem is the whole art:
[Platform notes](09-platform-notes.md) gives the values.
[Configuration](03-configuration.md) covers where it is set and which setting
wins; `0` demands an exact nanosecond match, and `--mtime-window <seconds>`
overrides everything for one run.

## What the rule cannot see

A change that leaves **both** the size and the modification time equal to the
record is invisible. It happens more often than it sounds: an editor or a sync
tool that restores modification times, a `cp -p` or `rsync -t` over a
same-size file, a write to the bucket that went around s3bak, or a torn upload
of a file that has since settled.

The demonstration is uncomfortably short. Edit a file, keep its length, put
its modification time back:

```console
$ s3bak status demo
$ s3bak verify demo
demo: OK (5 file record(s), 5 data object(s))
```

Both are content with the file. A push would be too — it uploads nothing, and
keeps uploading nothing for as long as the file sits there. What sees it is a
comparison that reads bytes:

```console
$ s3bak verify --checksum demo
s3bak: demo: content differs but size+mtime match: /home/you/demo/notes.txt (push will not upload it; use push --checksum)
demo: 1 error(s), 0 warning(s) (5 file record(s), 5 data object(s))
```

That is kept separate from an ordinary unpushed edit. A file whose size or
modification time drifted as well is simply a change the next push will take,
so it is named and then dismissed, without affecting the exit status:

```console
$ s3bak verify --checksum demo
demo: pending change: /home/you/demo/notes.txt (a push will upload it)
demo: OK (5 file record(s), 5 data object(s), 1 pending change(s))
```

Only the silent case is an error. `diff` finds it too, and shows what it is —
`a/` is the backup, `b/` is local:

```console
$ s3bak diff demo
--- a/notes.txt
+++ b/notes.txt
@@ -1,3 +1,3 @@
 first line
 second line
-third line
+THIRD line
```

The fix is a push that compares the same way:

```console
$ s3bak push --checksum demo
upload: /home/you/demo/notes.txt to s3://my-bucket/backup/demo/notes.txt
Updating s3://my-bucket/backup/demo-manifest.jsonl
```

### What `--checksum` does, and what it costs

`--checksum` replaces the size+mtime judgement with a content one: it
reconstructs each local file's S3 ETag and copies the pair when that does not
match the stored object's. So a same-size, same-mtime content change **is**
transferred, and a modification time that drifted on its own is **not**.

It reads and hashes every candidate file, which is why it is not the default.
On an entry of any size that is the difference between a push that reads
nothing and a push that reads everything. It also depends on the ETag being a
content digest, which is true of ordinary and multipart uploads but not of
objects encrypted with SSE-KMS; those fail the comparison loudly rather than
quietly comparing nonsense.

A smaller `mtime_window` is not an alternative. It closes only the case where
the modification time did move but landed inside the tolerance — never the
case where it did not move at all.

### Keeping it short-lived

Run `verify --checksum` on a schedule — weekly is a reasonable starting point
— and the blind spot has a bounded lifetime. It needs no write permission and
changes nothing, so it is safe to run from cron; a plain `verify` is cheap
enough to run daily. [Operating s3bak](07-operating.md) puts both into a
routine.

## What the other commands compare

The rule above pairs the manifest with the local tree, and `status`, `push`
and `pull` are built on it. The other commands pair things differently,
because an entry has more than one description:

- **the local tree** — what a walk of the entry's `path` finds right now;
- **the manifest** — what the last push recorded;
- **the S3 listing** — which objects exist under the entry's prefix, with
  their sizes and ETags.

| Command | What it answers | What it compares |
| --- | --- | --- |
| `status` | what a push would change | the manifest against the local tree |
| `push`, `pull` | what to transfer | the same, plus the S3 listing |
| `verify` | whether the backup holds what the manifest promises | the manifest against the S3 listing |
| `verify --checksum` | whether the stored bytes are the local bytes | local content against the stored ETag |
| `diff` | what changed inside a file | the stored content against the local content |

Only the bottom rows read content. `status` costs one S3 request — the
manifest download — and a local walk; it never lists the bucket.

The manifest also records each path's owner and group, but those are
informational. They are never compared, and a pull never applies them.

### Push sees what status cannot

Because `status` compares the manifest against the local tree, damage done to
the backup from the outside is invisible to it. `verify` is the command that
looks at the other pair:

```console
$ s3bak status demo
$ s3bak verify demo
s3bak: demo: size mismatch: s3://my-bucket/backup/demo/notes.txt (manifest 34, S3 37)
demo: 1 error(s), 0 warning(s) (5 file record(s), 5 data object(s))
```

An ordinary push repairs that without being told to. Its comparison has the S3
listing in hand, which carries every object's size for free, so a stored object
whose size no longer matches the record is re-uploaded from the local file.

### Self-healing, in one direction

A difference that turns out to be spurious settles itself. Touch a file
without editing it and the next push uploads it once, because size and
modification time are all the comparison has; that push records the new
modification time, and the run after it is quiet again.

The reverse does not hold, because a pull never rewrites the manifest. A record
that has gone stale — left by a push interrupted after its uploads, or by a
write to the bucket that went around s3bak — is something no pull can repair, so every pull that
transfers anything downloads that file again, and so does the one after it.
Only a push settles it. The asymmetry is deliberate: the manifest is the record
of what a push saw, so only a push may change it.

One thing hides this. A pull whose records all match the local tree already
returns immediately, without transferring anything at all, so a stale record
can sit unnoticed until some other difference gives that pull work to do.

## Excludes

An entry's `excludes` are glob patterns for paths to leave out. They work
exactly like `aws s3 sync --exclude` — the same pattern engine does the
matching — and the matching is deliberately literal:

- a relative pattern is compared against the **whole path relative to the
  entry root**, not one name at a time, and `*` matches across `/`. `*.elc`
  therefore excludes `*.elc` files at every depth;
- a pattern that starts at the filesystem root (`/home/you/...`, or a drive
  letter on Windows) is compared against the absolute path instead;
- every path is judged **on its own**. A directory is matched with a
  trailing `/` on its name: `cache/` matches the directory `cache` and
  nothing inside it, and a bare `cache` matches only a *file* named `cache`.
  Excluding a directory together with its contents is spelled `cache/*` —
  the `*` is allowed to match nothing, so the pattern covers the directory
  itself and everything below it.

The consequence worth remembering is that a pattern without a leading wildcard
is anchored at the entry root. `__pycache__/*` prunes the one at the top of
the entry and nothing else; `*/__pycache__/*` prunes every one below the top
and not the top itself. Covering both takes both patterns.

### What excluded means

An excluded path does not exist, as far as s3bak is concerned — on either
side. A push does not upload it and records nothing about it. A pull does not
restore it, does not overwrite it, and leaves its metadata alone; a
`pull --delete` never removes it; `status` does not mention it. Local or S3,
present or absent: ignored. The exceptions are `push --delete`, which can
retire what the backup still holds, and `verify`, which reports it — both
described below.

One visible consequence: an excluded directory is never recorded, so when a
pull restores a file inside one, the directory is created as a plain
directory — default permissions, nothing applied afterwards. s3bak does not
manage it.

### The backup keeps what it already holds

Excludes decide what s3bak touches from now on; they have no opinion about
what is already stored. A path excluded *after* it was backed up therefore
stays in the backup — record and object both — and every ordinary command
then ignores it: a push neither uploads nor deletes it, a pull does not
bring it back, and plain `status` shows nothing. The leftovers surface in
`status --delete`, the preview of the cleanup, and in `verify`'s routine
warning:

```console
$ s3bak verify demo
warning: demo: 2 recorded path(s) under excludes remain in the backup (push --delete retires them)
demo: 0 error(s), 1 warning(s) (5 file record(s), 5 data object(s))
```

Clearing them out is a deletion, and deletions are opt-in — see
[Deleting safely](06-deleting-safely.md). This is the intended path for
retiring an accidentally backed-up `node_modules`: add the exclude, then let
`push --delete` offer the leftovers. The local files are untouched either way;
being excluded, they are invisible to everything.

**An excluded path is never reported by plain `status`** — not as an `A`
(its local copy is invisible), and not as a `D` (plain status prints no `D`
at all). Under `status --delete` its leftovers print as `D`: the candidates
`push --delete` would offer.

**A pull does not bring it back.** The object may still sit in the backup,
but as long as the path is excluded, a pull skips it: deleted locally, it
stays deleted; present locally, it is not overwritten. To restore it, lift
the exclude first.

## Next

- [Command reference](05-command-reference.md) for `--checksum`,
  `--mtime-window` and the rest, command by command.
- [Deleting safely](06-deleting-safely.md) for turning a `D` into a removal.
- [Platform notes](09-platform-notes.md) for the `mtime_window` a given
  filesystem needs.
