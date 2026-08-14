# Deleting safely

s3bak deletes nothing unless you ask it to. A file you removed locally keeps
its backup; a file the backup does not know about keeps sitting in your tree.
Both are deliberate — a backup that mirrors every local mistake is not much of
a backup — and both mean the two sides drift apart until you say otherwise.

`--delete` is how you say otherwise. This chapter is what it does, how it asks,
and what it can cost you.

## Two directions, one flag

`--delete` always means "remove what the source no longer has". Which side is
the source depends on the command:

| Command | Source | What it removes |
| --- | --- | --- |
| `push --delete` | your local tree | objects and records the backup still holds |
| `pull --delete` | the backup | local files the backup does not record |

They are not opposites of each other so much as two different risks.
`push --delete` throws away a backup — recoverable only if the bucket has
versioning ([Operating s3bak](07-operating.md)). `pull --delete` throws away
local files, and one of them may be the only copy that exists.

Neither runs without confirmation, and neither is implied by anything else.
`--yes` is refused unless `--delete` is present, so there is no way to answer a
question you did not ask for.

## The confirmation prompt

Every candidate is one question. A one-line reminder of the answers comes
first, before the first question of the run:

```console
$ s3bak push --delete demo
s3bak: --delete answers: y=delete, n=keep, a=delete all, d=keep all, q=abort, ?=help
s3bak: demo: delete s3://my-bucket/backup/demo/old-notes.txt? [y/n/a/d/q/?] y
delete: s3://my-bucket/backup/demo/old-notes.txt
Updating s3://my-bucket/backup/demo-manifest.jsonl
```

| Answer | What it does |
| --- | --- |
| `y` | delete this one |
| `n` | keep this one |
| `a` | delete this one and everything after, without asking again |
| `d` | keep this one and everything after, without asking again |
| `q` | abort the whole command; nothing further is deleted |
| `?` | print the legend and ask again |

`yes`, `no`, `all` and `quit` are accepted in full. `delete` deliberately is
not: `d` means *keep*, and a tool that took `delete` for `d` would be waiting
to be misread. Anything else — a typo, a bare Enter — prints the legend and
asks the same question again, so a stray keystroke can never be taken for an
answer:

```console
s3bak: demo: delete s3://my-bucket/backup/demo/draft.txt? [y/n/a/d/q/?] ?
y, yes  - delete this one
n, no   - keep this one
a, all  - delete this one and everything after, without asking again
d       - keep this one and everything after, without asking again
q, quit - abort the whole command; nothing further is deleted
s3bak: demo: delete s3://my-bucket/backup/demo/draft.txt? [y/n/a/d/q/?] n
```

Reaching the end of standard input is not a typo, though — with no answer
channel left, a deletion flow stops rather than guesses, exactly as `q` would.

One question is asked at a time, and it holds the terminal until it is
answered: the transfer lines that would otherwise scroll it away wait and
print afterwards. Under `--all` the entries run one at a time for the same
reason, and every prompt names the entry it belongs to.

## `push --delete`: retiring a backup

Candidates arrive in ascending key order — the same order `status` reports
things in — and each is one of a few kinds.

**An object whose local file is gone** is the ordinary case, offered by its
S3 URL. Answering `n` keeps the object *and* its manifest record: the two
always travel together, so a kept backup stays a complete one. It then shows
up under `status --delete` until some later run retires it.

**A record with nothing behind it** is offered as the record itself. A
symlink, a special file and a directory are recorded but have no data object —
the record *is* the backup — so the prompt says which, and a confirmed drop
prints a `delete record:` line:

```console
$ s3bak push --delete demo
s3bak: --delete answers: y=delete, n=keep, a=delete all, d=keep all, q=abort, ?=help
s3bak: demo: delete s3://my-bucket/backup/demo/cache/a.bin? [y/n/a/d/q/?] y
s3bak: demo: delete s3://my-bucket/backup/demo/cache/b.bin? [y/n/a/d/q/?] y
s3bak: demo: delete s3://my-bucket/backup/demo/cache/ (directory record)? [y/n/a/d/q/?] y
delete record: s3://my-bucket/backup/demo/cache/
s3bak: demo: delete s3://my-bucket/backup/demo/link (symlink record)? [y/n/a/d/q/?] y
delete record: s3://my-bucket/backup/demo/link
delete: s3://my-bucket/backup/demo/cache/a.bin
delete: s3://my-bucket/backup/demo/cache/b.bin
Updating s3://my-bucket/backup/demo-manifest.jsonl
```

Two things to read out of that. A directory is asked **after** everything
inside it, because a directory record is only worth dropping once nothing
restores into it. And the `delete:` lines for the objects arrive at the end
rather than after each `y`: deletions are batched — up to a thousand keys per
request — so the answers are collected first and the removals reported as they
are actually sent. A record drop needs no request, so its line prints
immediately.

Keeping any one child keeps its directory silently. There is no question for
the directory at all, because dropping its record would strip the recorded
permissions and timestamp from a directory the survivor still needs:

```console
$ s3bak push --delete demo
s3bak: --delete answers: y=delete, n=keep, a=delete all, d=keep all, q=abort, ?=help
s3bak: demo: delete s3://my-bucket/backup/demo/cache/a.bin? [y/n/a/d/q/?] y
s3bak: demo: delete s3://my-bucket/backup/demo/cache/b.bin? [y/n/a/d/q/?] n
delete: s3://my-bucket/backup/demo/cache/a.bin
Updating s3://my-bucket/backup/demo-manifest.jsonl

$ s3bak status --delete demo
D /home/you/demo/cache
D /home/you/demo/cache/b.bin
```

**An object the manifest does not record** is flagged, because it is the one
candidate s3bak cannot describe:

```console
s3bak: demo: delete s3://my-bucket/backup/demo/stray.txt (not in manifest)? [y/n/a/d/q/?] n
```

Such an object is an out-of-band upload, or what an interrupted push left
behind. Answering `n` keeps it but cannot record it — a record describes a
local file, and there is none — so the same question comes back on every later
`--delete`, and `verify` keeps reporting it. Either delete it here, or make it
legitimate by putting the file back locally and pushing.

A **file replaced by a symlink** (or by a special file) arrives here too, and
it is the case most likely to surprise. An ordinary push handles the change
itself — it re-records the path as a symlink — but removing the file's old
data object would be a deletion, so that object stays. From then on the
manifest and the bucket disagree at that key, and `verify` reports it as an
error rather than a warning:

```console
$ s3bak verify demo
s3bak: demo: type conflict: s3://my-bucket/backup/demo/draft.txt (manifest records a symlink, but a data object exists at its key)
demo: 1 error(s), 0 warning(s) (5 file record(s), 6 data object(s))
```

`status --delete` shows nothing for it — there is no leftover *record*, only a
leftover object — so `push --delete` is the only command that offers it, and
it does so flagged `(not in manifest)`. A confirmed deletion removes just the
object; the symlink's record stays, and `verify` goes quiet.

**A record whose object is already gone needs no `--delete` at all.** It
describes a backup that does not exist — a pull could restore nothing from it
— so retiring it is repair rather than deletion, and any ordinary push does it
silently.

### Rehearsing first

`status --delete` lists what a `push --delete` would offer, and removes
nothing:

```console
$ s3bak status --delete demo
M /home/you/demo	mtime
D /home/you/demo/cache
D /home/you/demo/cache/a.bin
D /home/you/demo/cache/b.bin
D /home/you/demo/link
```

It is a cheap preview drawn from the manifest alone, which is also its limit:
it never lists the bucket, so an object the manifest does not record — the
`(not in manifest)` case above — cannot appear in it. The exact rehearsal,
with the real listing and no questions, is `push --delete --dry-run`:

```console
$ s3bak push --delete --dry-run demo
(dry-run) delete: s3://my-bucket/backup/demo/draft.txt
(dry-run) would update manifest: demo-manifest.jsonl
```

### Cleaning up after a new exclude

This is the reason `push --delete` treats excluded paths as candidates at all.
Excludes decide what s3bak touches from now on; they say nothing about what is
already stored, so a path excluded *after* it was backed up stays in the
backup, invisible to every ordinary command. `verify` is what tells you:

```console
$ s3bak verify demo
warning: demo: 3 recorded path(s) under excludes remain in the backup (push --delete retires them)
demo: 0 error(s), 1 warning(s) (6 file record(s), 6 data object(s))
```

An ordinary push does nothing about it. `push --delete` offers the leftovers,
and `a` takes the lot:

```console
$ s3bak push --delete demo
s3bak: --delete answers: y=delete, n=keep, a=delete all, d=keep all, q=abort, ?=help
s3bak: demo: delete s3://my-bucket/backup/demo/cache/a.bin? [y/n/a/d/q/?] a
delete record: s3://my-bucket/backup/demo/cache/
delete: s3://my-bucket/backup/demo/cache/a.bin
delete: s3://my-bucket/backup/demo/cache/b.bin
Updating s3://my-bucket/backup/demo-manifest.jsonl

$ s3bak verify demo
demo: OK (4 file record(s), 4 data object(s))
```

The local files are untouched throughout — being excluded, they are invisible
to s3bak on that side. This is the supported way to retire an accidentally
backed-up `node_modules`: add the exclude, then let one `push --delete` clear
what it left behind.

### Removing the backup of a sub-path

Pointing a push at a sub-path whose local tree is gone is refused, since a
typo and a deliberate removal look identical:

```console
$ s3bak push demo/lib
s3bak: local path does not exist (use --delete to remove its backup): /home/you/demo/lib
```

With `--delete` it becomes one question for the whole subtree, rather than one
per file:

```console
$ s3bak push --delete demo/lib
s3bak: demo: delete the backup subtree s3://my-bucket/backup/demo/lib? [y/n] n
s3bak: backup subtree not deleted (answer y, or use --yes): s3://my-bucket/backup/demo/lib
```

Declining leaves the command a failure (exit 1): you asked it to deal with
that path and it did not. Confirming removes exactly that key and everything
below `lib/` — never a sibling that merely starts with the same letters — and
drops the subtree from the manifest.

### When the local scan was incomplete

If the walk could not read part of the tree — an unopenable directory, a file
that vanished mid-walk — every deletion candidate from that point on is kept,
and the push warns:

```console
warning: demo: the local scan skipped unreadable or vanished paths; kept 3 deletion candidate(s) and every manifest record
```

A path missing from a partial view is indistinguishable from a path that was
deleted, and the wrong guess destroys a good backup. Fix what the walk could
not read, then run `push --delete` again.

## `pull --delete`: mirroring the backup locally

`pull --delete` removes local files the manifest does not record, which turns
a restore into an exact mirror. The questions look the same, and the removals
report as they happen:

```console
$ s3bak pull --delete demo
755 /home/you/demo
s3bak: --delete answers: y=delete, n=keep, a=delete all, d=keep all, q=abort, ?=help
s3bak: demo: delete /home/you/demo/never-pushed.txt? [y/n/a/d/q/?] n
s3bak: demo: delete /home/you/demo/scratch/x.txt? [y/n/a/d/q/?] y
delete: /home/you/demo/scratch/x.txt
s3bak: demo: delete /home/you/demo/scratch? [y/n/a/d/q/?] y
delete: /home/you/demo/scratch
755 /home/you/demo
```

Extras are offered subtree by subtree, children before the directory that
holds them, so a directory is only ever removed once it is empty. Keeping
something keeps every directory above it too, without asking about them — they
could not be removed anyway.

The metadata line appears twice because every removal changes its parent
directory's modification time; s3bak re-applies the recorded metadata to
exactly the directories the removals disturbed.

An excluded local path is invisible to this diff and is never offered. Neither
is a name your filesystem might fold onto a recorded one — a case difference on
Windows or macOS, a Unicode spelling difference, a trailing dot Windows trims.
Such a name might *be* the file the pull just restored under its recorded
spelling, so it is reported rather than removed:

```console
warning: not removed (a local name the filesystem may fold onto a recorded path): /home/you/demo/README.TXT
```

Finally, the mirror is skipped entirely if applying the metadata failed. A
tree that is not in its recorded state is not a tree whose extras can be
trusted.

### An extra can be the only copy

This is the one place where s3bak can destroy data that exists nowhere else,
and it deserves a moment's thought before you type `a`.

An extra is judged solely by the manifest. A file that was **never pushed** —
created since the last backup, or living in the tree but excluded from an
earlier push and later un-excluded — is an extra. So is a file whose push was
interrupted before the manifest was written: its object may be sitting on S3
unrecorded, or may never have been uploaded at all. In every one of those
cases the file is real work, and the backup does not hold it.

```console
$ s3bak pull --delete demo
s3bak: demo: delete /home/you/demo/never-pushed.txt? [y/n/a/d/q/?] n
```

The per-item prompt exists for exactly this. Two habits make it safe:

- **`push` before you `pull --delete`.** Anything worth keeping is then in
  the backup, and is no longer an extra.
- **Rehearse.** `pull --delete --dry-run` lists every candidate without asking
  or removing, and `status` names the local-only files as `A` lines beforehand.

## Unattended runs

`--yes` answers yes to every question. It is the flag that makes a mirror
possible from cron, and it is also the flag that removes the safety net:

```console
$ s3bak push --delete --yes demo
delete: s3://my-bucket/backup/demo/draft.txt
Updating s3://my-bucket/backup/demo-manifest.jsonl
```

Without a terminal on both standard input and standard error, `--delete`
without `--yes` answers **no** to everything. Nothing is deleted and the run
still succeeds:

```console
$ s3bak push --delete demo < /dev/null
Updating s3://my-bucket/backup/demo-manifest.jsonl
```

That is a valid outcome, not a failure — "no" is an answer — so a scheduled
`push --delete` that nobody added `--yes` to does the backup and quietly keeps
everything. Whether that is what you wanted is worth checking with
`status --delete` from time to time. [Operating s3bak](07-operating.md) puts
this into a routine.

## When you answer `q`

`q` aborts the whole command, exactly as an S3 error or `Ctrl-C` would, and
exits 1. For a push, the manifest is not rewritten and `post_hook` does not
run:

```console
$ s3bak push --delete demo
s3bak: --delete answers: y=delete, n=keep, a=delete all, d=keep all, q=abort, ?=help
s3bak: demo: delete s3://my-bucket/backup/demo/draft.txt? [y/n/a/d/q/?] y
s3bak: demo: delete s3://my-bucket/backup/demo/old-notes.txt? [y/n/a/d/q/?] q
s3bak: demo: aborted; the manifest was not rewritten, so it may no longer match S3 - push this entry again to settle it
```

An abort is a stop, not a rollback, and two things may already have happened.
A request in flight finishes — s3bak cancels what has not started, but tearing
down a transfer mid-request would strand an abandoned multipart upload — so an
upload or two can land unrecorded. And a deletion you confirmed may or may not
have run, because deletions batch: an object answered `y` is gone if its batch
had already been sent, and untouched if it had not.

Both settle on the next ordinary push of that entry — no `--delete` needed. It
records what was uploaded and retires the records whose objects are gone. The
message says so, and under `--all` the entries the abort never reached are
named too.

`pull --delete` aborts the same way, reporting how far it got:

```console
s3bak: demo: aborted; the local tree was updated only as far as the answers went - pull again to finish it
```

## What the exit status says

| Situation | Exit |
| --- | --- |
| everything answered `n`, including a non-interactive all-no run | 0 |
| deletions carried out | 0 |
| `q`, or reaching the end of input at a prompt | 1 |
| a declined subtree deletion (`push --delete entry/sub`) | 1 |

The first row is worth remembering when reading logs: a `--delete` run that
deleted nothing at all looks exactly like a successful one, because it is.

## Next

- [Operating s3bak](07-operating.md) for where `--delete` belongs in a
  routine, and for bucket versioning as the net under a wrong `y`.
- [Recovery and troubleshooting](08-recovery-troubleshooting.md) for settling
  what an interrupted run left behind.
- [Command reference](05-command-reference.md) for the options themselves.
