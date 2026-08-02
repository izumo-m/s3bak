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
`A` exists only locally, `D` only in the backup. Silence means everything
matched.

A path whose *type* changed reports a replacement rather than a modification,
and how it prints depends on the new type. A regular file replaced by a
symlink is a single `D`, because the two still occupy the same key:

```console
$ s3bak status demo
M /home/you/demo/lib	mtime
D /home/you/demo/lib/util.sh
```

A regular file replaced by a **directory** prints both letters for the one
path — the record and the local directory sort to different keys, so nothing
pairs them up:

```console
$ s3bak status demo
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

One habit widens the blind spot rather than narrowing it: `push --meta-only`
records the local state as though it had been uploaded, without uploading it.
A local edit that was never pushed becomes invisible to every later size+mtime
comparison. See [Command reference](05-command-reference.md) before reaching
for it.

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
that has gone stale — left by `push --data-only`, or by a write to the bucket
that went around s3bak — is something no pull can repair, so every pull that
transfers anything downloads that file again, and so does the one after it.
Only a push settles it. The asymmetry is deliberate: the manifest is the record
of what a push saw, so only a push may change it.

One thing hides this. A pull whose records all match the local tree already
returns immediately, without transferring anything at all, so a stale record
can sit unnoticed until some other difference gives that pull work to do.

## Excludes

An entry's `excludes` are glob patterns for paths to leave out. Each is
matched against the path relative to the entry root, and the matching is
deliberately literal:

- a pattern is compared against the **whole relative path**, not one name at a
  time, and `*` matches across `/`. `*.elc` therefore excludes `*.elc` files
  at every depth;
- a pattern ending in `/*` names a **directory** and prunes it: the walk never
  descends, so an excluded subtree costs nothing to skip;
- anything else is matched against individual paths.

The consequence worth remembering is that a pattern without a leading wildcard
is anchored at the entry root. `__pycache__/*` prunes the one at the top of
the entry and nothing else; `*/__pycache__/*` prunes every one below the top
and not the top itself. Covering both takes both patterns.

### Excludes prune the local side only

The S3 listing is never filtered. This has consequences that surprise people,
and all of them follow from that one sentence.

**A path excluded after it was backed up stays in the backup.** Its record and
its object are still there, and since the local walk no longer produces the
path, the comparison sees a record with nothing beside it:

```console
$ s3bak status demo
D /home/you/demo/cache
D /home/you/demo/cache/tmp.bin
```

An ordinary push keeps both, as it keeps any `D`. Clearing them out is a
deletion, and deletions are opt-in — see
[Deleting safely](06-deleting-safely.md). This is the intended path for
retiring an accidentally backed-up `node_modules`: add the exclude, then let
`push --delete` offer the leftovers. The local files are untouched either way;
being excluded, they are invisible to the walk that would have looked at them.

**An excluded path is never reported as `A`.** It is not compared at all, so
it cannot show up as something a push would add.

**A pull still restores it.** The download side is driven by the backup, not
by the local walk, so an object that is in the backup comes back whether or
not the current configuration would have uploaded it:

```console
$ rm /home/you/demo/cache/tmp.bin
$ s3bak pull demo
download: s3://my-bucket/backup/demo/cache/tmp.bin to /home/you/demo/cache/tmp.bin
644 /home/you/demo/cache/tmp.bin
755 /home/you/demo/cache
```

Excludes decide what gets backed up. They do not decide what gets restored,
and they have no opinion at all about what is already stored.

## Next

- [Command reference](05-command-reference.md) for `--checksum`,
  `--mtime-window`, `--meta-only` and the rest, command by command.
- [Deleting safely](06-deleting-safely.md) for turning a `D` into a removal.
- [Platform notes](09-platform-notes.md) for the `mtime_window` a given
  filesystem needs.
