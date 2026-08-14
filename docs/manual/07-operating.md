# Operating s3bak

Everything so far has been one command at a time, typed and watched. Operating
s3bak is the rest of it: what runs on a schedule, what becomes of the output
when nobody is reading it, and what the bucket does with the objects after
they arrive.

None of this is needed to have a backup. It is what turns a backup you made
once into one you can still trust in a year.

## A routine

| Command | How often | What it is for |
| --- | --- | --- |
| `s3bak push --all` | daily | the backup itself |
| `s3bak verify --all` | daily | proves the stored objects still match the manifest |
| `s3bak verify --checksum --all` | weekly | closes the size+mtime blind spot |
| `s3bak status --delete --all` | monthly | shows what a scheduled push is keeping forever |
| `s3bak push --delete` | by hand, when the above is what you meant | retires those backups |
| a restore drill | quarterly, and after changing entries or excludes | proves a restore works |

The push and the two `verify` runs belong in a scheduler. What follows them in
the table wants a person, which is the whole reason it is not part of the
push.

### The daily push

```console
$ s3bak push --all
upload: /etc/wsl.conf to s3://my-bucket/backup/wsl.conf
Updating s3://my-bucket/backup/wsl.conf-manifest.jsonl
upload: /home/you/demo/lib/util.sh to s3://my-bucket/backup/demo/lib/util.sh
upload: /home/you/demo/todo.txt to s3://my-bucket/backup/demo/todo.txt
upload: /home/you/demo/notes.txt to s3://my-bucket/backup/demo/notes.txt
Updating s3://my-bucket/backup/demo-manifest.jsonl
upload: /home/you/vault/keys.kdbx to s3://my-bucket/backup/vault/keys.kdbx
Updating s3://my-bucket/backup/vault-manifest.jsonl
```

Entries are processed several at a time — `entry_concurrency`, four by
default — so their lines interleave and the order changes between runs. The
exit status does not: it is the first non-zero one in sorted entry order, not
whichever entry failed first.

The second run of the same command, with nothing changed, prints nothing at
all:

```console
$ s3bak push --all
$
```

Silence is the normal result of a backup that is up to date, which is what
makes a scheduled push worth reading: anything in the log is something that
happened. A quiet push is cheap on the S3 side as well — per entry, one
manifest download, and for a directory entry one listing request per thousand
objects. The work it does do is local: walking the tree and comparing every
path against the manifest.

Push as often as the machine is on and the link can stand. Nothing about
s3bak assumes daily; daily is simply the cadence at which the answer to "how
much work would I lose" stays comfortable.

### Watching that the backup is still good

A push proves that s3bak uploaded what it found. It does not prove that what
was uploaded is still there. `verify` is the command that asks S3 directly:

```console
$ s3bak verify --all
vault: OK (1 file record(s), 1 data object(s))
wsl.conf: OK (1 file record(s), 1 data object(s))
demo: OK (3 file record(s), 3 data object(s))
```

Every entry prints a line whether or not it found anything, so a quiet log
still shows the check ran. It reads the manifest and lists the objects — no
downloads, no writes — which makes it safe to run from a scheduler and safe to
run with a credential that has no write permission at all. It needs no local
copy of the tree either, so a second machine can be the one that watches.

Once a week, `--checksum` adds the part a listing cannot see:

```console
$ s3bak verify --checksum --all
vault: OK (1 file record(s), 1 data object(s))
demo: OK (3 file record(s), 3 data object(s))
wsl.conf: OK (1 file record(s), 1 data object(s))
```

That run compares every recorded file's local content against the ETag the
listing already delivered: no extra S3 requests, but it reads and hashes the
whole local tree, and it only works where the tree actually lives. What it
finds is the file whose content changed while its size and modification time
did not — the blind spot [How s3bak detects
changes](04-change-detection.md) describes, which nothing else reports before
a restore does.

What each finding means, and what settles it, is [Recovery and
troubleshooting](08-recovery-troubleshooting.md).

### The deletions nobody confirmed

A scheduled push never deletes anything. That is deliberate, and it means the
backup accumulates: every file you delete locally stays on S3 until you say
otherwise. `status --delete` is how to see the accumulation without touching
it:

```console
$ s3bak status --delete --all
M /home/you/demo	mtime
D /home/you/demo/todo.txt
```

The `D` line is what a `push --delete` would offer to remove. An ordinary push
settles the `M` and leaves the `D` exactly where it was:

```console
$ s3bak push --all
Updating s3://my-bucket/backup/demo-manifest.jsonl
$ s3bak status --delete --all
D /home/you/demo/todo.txt
```

So the `D` lines are a list that only grows until a person looks at it. Read
it monthly, or after any large deletion or exclude change, and then run
`push --delete` by hand for the entries whose lines are what you meant.
[Deleting safely](06-deleting-safely.md) is that command in full.

### A restore you have actually done

`verify` proves the pieces are present and intact. It does not prove that a
restore produces a usable tree, or that the thing you backed up was the thing
worth backing up. Only a restore proves that:

```sh
s3bak pull demo -o /tmp/drill
```

`-o` puts the entry somewhere harmless, so the drill cannot damage the real
tree ([Command reference](05-command-reference.md)). Look at what comes out —
open the files, run the program, load the database dump. A quarterly drill is
enough; run one whenever entries, excludes, or the bucket policy change, since
those are the changes that quietly alter what is in the backup.

## Running unattended

### cron

```crontab
S3BAK_CONFIG=/home/you/.config/s3bak/config.py

30 2 * * *  /home/you/.local/bin/s3bak push --all       >> /home/you/.local/state/s3bak.log 2>&1
15 3 * * *  /home/you/.local/bin/s3bak verify --all     >> /home/you/.local/state/s3bak.log 2>&1
15 4 * * 0  /home/you/.local/bin/s3bak verify --checksum --all >> /home/you/.local/state/s3bak.log 2>&1
```

The `S3BAK_CONFIG` line is only needed if the configuration is not at
`~/.config/s3bak/config.py`. The absolute path to the executable is needed
either way, because cron's `PATH` is not your shell's; `command -v s3bak`
prints the path to write here.

Redirecting both streams into one file keeps the ordering of what s3bak
printed: transfers go to standard output, warnings and errors to standard
error. Without the redirect, cron mails you the output instead, which is a
reasonable choice when the mail actually gets delivered.

### systemd timer

A user timer replaces the cron line and puts the output in the journal:

```ini
# ~/.config/systemd/user/s3bak.service
[Unit]
Description=s3bak push

[Service]
Type=oneshot
ExecStart=%h/.local/bin/s3bak push --all
```

```ini
# ~/.config/systemd/user/s3bak.timer
[Unit]
Description=Daily s3bak push

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

`systemctl --user enable --now s3bak.timer` starts it, `journalctl --user -u
s3bak` reads what it printed, and `systemctl --user start s3bak.service` runs
it once by hand. `Persistent=true` makes a missed run happen at the next boot
rather than being skipped, which matters on a machine that is not always on.
A user unit stops when you log out unless `loginctl enable-linger` is set for
the account.

### What a scheduled run does not inherit

- **`PATH`.** Name the s3bak executable absolutely, and name a hook's program
  absolutely too: a hook is run without a shell, but its first item is still
  looked up along `PATH`, and the minimal `PATH` a scheduler provides is a
  poor place to look.
- **`HOME`.** Both the default configuration path and `~/.aws` are found
  through it. cron sets it; a system-level service unit may not.
- **The AWS profile.** It is read from the `~/.aws/config` of whichever user
  the job runs as. A backup that works when you type it and fails from a
  root-owned schedule is almost always this.
- **A terminal.** Colour switches itself off, and `--delete` without `--yes`
  answers no to everything rather than blocking on a prompt nobody can see
  ([Deleting safely](06-deleting-safely.md)).

### Two runs at once

s3bak takes no lock. Two pushes of one entry running at the same time will
interleave their uploads and then publish two manifests, the second describing
a tree the first has already changed. Nothing is corrupted, but the surviving
manifest can disagree with the objects until the next push settles it.

Under cron, keep the runs apart with `flock`:

```sh
flock -n /home/you/.local/state/s3bak.lock /home/you/.local/bin/s3bak push --all
```

`-n` makes a run that finds the lock held give up immediately instead of
queueing behind a push that is still going.

A systemd timer needs nothing: the service unit will not start a second copy
of itself while the first is still running.

### Reading the exit status

| Exit | What a scheduled run should do |
| --- | --- |
| 0 | nothing; the work is done |
| 1 | look — something prevented the result |
| 2 | look — the work was done, but something warned |
| 3 and up | a hook failed, with its own status |
| 130 | interrupted |

Exit 2 is the one worth wiring up, because it is the one that looks like
success from a distance: the backup happened, and something about it deserves
a human. An unreadable directory skipped during a push, a stale record skipped
during a pull, anything `verify` warned about.

```sh
#!/bin/sh
/home/you/.local/bin/s3bak push --all
status=$?
[ "$status" -eq 0 ] || echo "s3bak push exited $status" | mail -s "s3bak" you@example.com
exit "$status"
```

Under `--all`, one entry's failure does not stop the others. The failing entry
reports and the rest go on doing their work:

```console
$ s3bak push --all
s3bak: target does not exist: /home/you/vault
upload: /etc/wsl.conf to s3://my-bucket/backup/wsl.conf
Updating s3://my-bucket/backup/wsl.conf-manifest.jsonl
upload: /home/you/demo/notes.txt to s3://my-bucket/backup/demo/notes.txt
Updating s3://my-bucket/backup/demo-manifest.jsonl
```

That run exits 1 for the entry that failed, having backed up every entry that
could.

### Unattended deletion

A mirror that removes what you removed needs `--yes`, since there is nobody to
answer the questions. Rehearse it with `--dry-run` first, and read
[Deleting safely](06-deleting-safely.md) before scheduling it at all: `--yes`
is the flag that turns the confirmation prompt off, and the prompt is the last
thing standing between a local mistake and the backup of what it destroyed.

## Hooks

An entry's `pre_hook` runs before every push attempt, and its `post_hook`
after a push that did work. Both are argument vectors — the program, then its
arguments — run without a shell. [Configuration](03-configuration.md) covers
how to write them; this section is what they behave like in a running system.

- **Standard input is detached.** A hook cannot read the terminal, which is
  what stops it from swallowing the answer to a `--delete` question being
  asked on another thread.
- **Output goes where s3bak's goes.** A hook writing to standard output lands
  in the same log, interleaved with the transfer lines.
- **The exit status propagates**, with two adjustments: 2 becomes 1, because
  s3bak reserves 2 for its own warnings, and a hook killed by signal *N*
  becomes 128+*N*.
- **Entries push concurrently**, so one entry's hook may run while another
  entry is still transferring. Hooks of different entries have no order
  between them.

`-v` shows each hook as it is about to run:

```console
$ s3bak push -v demo
+ pre_hook: ['/home/you/bin/dump-db']
dumped /home/you/demo/db.sql
+ (boto3) get_object s3://my-bucket/backup/demo-manifest.jsonl
+ (boto3-s3) sync /home/you/demo s3://my-bucket/backup/demo/
upload: /home/you/demo/db.sql to s3://my-bucket/backup/demo/db.sql
upload: /home/you/demo/notes.txt to s3://my-bucket/backup/demo/notes.txt
Updating s3://my-bucket/backup/demo-manifest.jsonl
+ (boto3) put_object s3://my-bucket/backup/demo-manifest.jsonl
+ post_hook: ['/home/you/bin/offsite']
offsite: 2 changed file(s)
```

The hook's own output — `dumped ...`, `offsite: ...` — is there without `-v`;
what `-v` adds is the `+` line naming the argument vector about to run.

### `pre_hook`: making the thing you back up

A `pre_hook` runs before s3bak has looked at anything, so it can create the
file or tree that is about to be backed up. That is what makes it the answer
to a live database: dump it, back up the dump. A file being written while the
push reads it can upload half-written, and the dump is the way not to find
that out during a restore.

If the hook fails, the push does not happen and the status is the hook's own:

```console
$ s3bak push demo
cannot reach the database
s3bak: pre_hook failed (exit 3): ['/home/you/bin/dump-db-broken']
```

The old backup is left exactly as it was, which is the right outcome: a failed
dump would otherwise be backed up as an empty or truncated file, overwriting
the last good one.

One consequence is worth planning for. A dump written unconditionally is a new
file every run — new modification time, usually a new size — so every push
transfers it in full and every push therefore counts as work. If that matters
(a large dump, a metered link, a versioned bucket keeping every copy), have
the hook write the dump only when the content actually changed.

### `post_hook`: only after a push that did work

A push that changes nothing runs no `post_hook` at all. The same command, run
twice:

```console
$ s3bak push vault
upload: /home/you/vault/keys.kdbx to s3://my-bucket/backup/vault/keys.kdbx
Updating s3://my-bucket/backup/vault-manifest.jsonl
offsite: 1 changed file(s)
$ s3bak push vault
$
```

That is deliberate. A hook with side effects — an off-site copy, a
notification — should not fire on a backup that did not happen. When you want
it anyway, `s3bak hook post <entry>` runs it on demand.

A failing `post_hook` is a different kind of failure from a failing
`pre_hook`. It runs after the manifest is published, so the backup is already
complete and only the hook's own effect is missing:

```console
$ s3bak push demo
upload: /home/you/demo/notes.txt to s3://my-bucket/backup/demo/notes.txt
Updating s3://my-bucket/backup/demo-manifest.jsonl
s3bak: post_hook failed (exit 2): ['/home/you/bin/hook-two']
```

That run exits 1 — the hook's own 2 normalized, since 2 means "warnings" from
s3bak itself. Re-running the hook is `s3bak hook post demo`; re-running the
push would not, because there is nothing left for it to do.

### `S3BAK_JOURNAL`

A `post_hook` after a directory push finds `S3BAK_JOURNAL` in its environment,
naming a file that says exactly what the push changed. Without it a hook has
to assume everything may have moved; with it, an off-site copy or a
notification can be about the actual changes.

The file has one line per changed path: a single marker character, then the
manifest record as JSON.

| Marker | Meaning |
| --- | --- |
| `+` | the path is new to the backup |
| `!` | the path was already recorded and its record changed |
| `-` | the record was dropped — a confirmed deletion, or a record whose object was already gone |
| (space) | no change — the line a directory record gets while its removal is still being decided, left behind when the directory is kept |

A first push journals `+` for everything it recorded, the entry root
(`"path":"."`) included. The `post_hook` below is a script that prints the
file it was handed, so what follows the manifest line is the journal itself:

```console
$ s3bak push demo
dumped /home/you/demo/db.sql
upload: /home/you/demo/db.sql to s3://my-bucket/backup/demo/db.sql
upload: /home/you/demo/lib/util.sh to s3://my-bucket/backup/demo/lib/util.sh
upload: /home/you/demo/notes.txt to s3://my-bucket/backup/demo/notes.txt
upload: /home/you/demo/old-notes.txt to s3://my-bucket/backup/demo/old-notes.txt
Updating s3://my-bucket/backup/demo-manifest.jsonl
+{"path":".","mode":"40755","owner":"you","group":"you","mtime_ns":1786679899398566242}
+{"path":"./db.sql","mode":"100644","owner":"you","group":"you","size":18,"mtime_ns":1786679899405132457}
+{"path":"./lib","mode":"40755","owner":"you","group":"you","mtime_ns":1786679899222385089}
+{"path":"./lib/util.sh","mode":"100644","owner":"you","group":"you","size":5,"mtime_ns":1786679899222398222}
+{"path":"./notes.txt","mode":"100644","owner":"you","group":"you","size":6,"mtime_ns":1786679899222367037}
+{"path":"./old-notes.txt","mode":"100644","owner":"you","group":"you","size":4,"mtime_ns":1786679899222415969}
```

A later push journals only what it touched. This one edited `notes.txt`, added
`lib/new.sh`, ran `chmod +x` on `lib/util.sh`, deleted `old-notes.txt` under
`--delete --yes`, and — as every push of this entry does — had its `pre_hook`
rewrite `db.sql`:

```console
$ s3bak push --delete --yes demo
dumped /home/you/demo/db.sql
upload: /home/you/demo/db.sql to s3://my-bucket/backup/demo/db.sql
delete: s3://my-bucket/backup/demo/old-notes.txt
upload: /home/you/demo/notes.txt to s3://my-bucket/backup/demo/notes.txt
upload: /home/you/demo/lib/new.sh to s3://my-bucket/backup/demo/lib/new.sh
Updating s3://my-bucket/backup/demo-manifest.jsonl
!{"path":".","mode":"40755","owner":"you","group":"you","mtime_ns":1786679899443190708}
!{"path":"./db.sql","mode":"100644","owner":"you","group":"you","size":18,"mtime_ns":1786679899490127386}
!{"path":"./lib","mode":"40755","owner":"you","group":"you","mtime_ns":1786679899443124516}
+{"path":"./lib/new.sh","mode":"100644","owner":"you","group":"you","size":4,"mtime_ns":1786679899443190708}
!{"path":"./lib/util.sh","mode":"100755","owner":"you","group":"you","size":5,"mtime_ns":1786679899222398222}
!{"path":"./notes.txt","mode":"100644","owner":"you","group":"you","size":15,"mtime_ns":1786679899443124516}
-{"path":"./old-notes.txt","mode":"100644","owner":"you","group":"you","size":4,"mtime_ns":1786679899222415969}
```

Reading it correctly means keeping a few things in mind:

- **The paths are entry-relative**, spelled from the entry root as `./name`,
  in the same order the manifest uses. Join them onto the entry's `path` to
  get local paths, or onto the entry's prefix to get S3 keys.
- **Not every line moved bytes.** The two directories above changed only their
  own modification times, and `lib/util.sh` changed only its permission bits;
  neither was uploaded. The `mode` field is the full octal value, so its
  leading digits give the kind — `100` a regular file, `40` a directory, `120`
  a symlink — and only a regular file has an object behind it.
- **The record is the new state**, except after `-`, where it is the record
  being dropped.
- **The file lives only as long as the hook does.** s3bak deletes it as soon
  as the hook returns, so anything you need afterwards must be copied out
  first.

A hook that counts what changed is a few lines:

```python
#!/usr/bin/env python3
import json
import os
import sys

path = os.environ.get("S3BAK_JOURNAL")
if not path:
    print("offsite: full copy (no journal)")
    sys.exit(0)

changed = []
with open(path, encoding="utf-8") as f:
    for line in f:
        marker, record = line[0], json.loads(line[1:])
        if marker in "+!" and record["mode"].startswith("100"):
            changed.append(record["path"])
print(f"offsite: {len(changed)} changed file(s)")
```

`S3BAK_JOURNAL` is unset for a push that runs no comparison — a single-file
entry, the deletion of a sub-path's whole subtree — and for `s3bak hook post`,
which runs the hook outside any push at all:

```console
$ s3bak hook post vault
offsite: full copy (no journal)
```

Unset always means the same thing: no per-file detail is available, so assume
anything may have changed. A hook that handles that case is a hook you can
also run by hand.

### Running a hook on its own

`s3bak hook pre <entry>` and `s3bak hook post <entry>` run one configured hook
outside any push — re-running an off-site copy after the far side changed,
testing a dump script before trusting a backup to it, or finishing what a
failed `post_hook` did not. The contract is identical to a push's, minus the
journal. [Command reference](05-command-reference.md) covers the command,
including what `--all` does with entries that configure no such hook.

## The bucket side

What follows is decided on the bucket rather than in s3bak, which manages none
of it — and each decision changes what the backup is worth.

### Storage classes

s3bak uploads with whatever storage class the bucket applies by default —
`STANDARD` unless you arranged otherwise — and has no setting for anything
else. Objects move to another class because a lifecycle rule moves them.

An archived class (`GLACIER`, `DEEP_ARCHIVE`) is not a copy you can read.
`verify` reports every archived object it lists, as a warning rather than an
error — the backup still holds what the manifest promises, it is just not
available right now. (Glacier Instant Retrieval is not one of these: it reads
like any other object, so s3bak has nothing to say about it.)

```console
$ s3bak verify demo
warning: demo: archived storage class GLACIER: s3://my-bucket/backup/demo/notes.txt (a pull cannot fetch it until RestoreObject completes)
demo: 0 error(s), 1 warning(s) (4 file record(s), 4 data object(s))
```

A pull of that entry fetches everything else and fails on that one object:

```console
$ s3bak pull demo
download: s3://my-bucket/backup/demo/db.sql to /home/you/demo/db.sql
download: s3://my-bucket/backup/demo/lib/util.sh to /home/you/demo/lib/util.sh
download: s3://my-bucket/backup/demo/lib/new.sh to /home/you/demo/lib/new.sh
notes.txt: An error occurred (InvalidObjectState) when calling the GetObject operation: The operation is not valid for the object's storage class
1 of 4 operations failed
```

Getting it back takes a restore request to S3 itself (`aws s3api
restore-object`, or Initiate Restore in the console), which makes a temporary
copy available after minutes or hours depending on the class and the retrieval
tier you ask for. Once it is ready, pull again.

That is the trade an archive rule makes, and it is worth being deliberate
about:

- **Restores stop being self-service.** Every recovery gains a wait, exactly
  when you are least in the mood for one.
- **Small files pay badly.** Every class below `STANDARD` bills a minimum
  storage duration per object — currently 90 days for Glacier Flexible
  Retrieval, 180 for Deep Archive, 30 for the infrequent-access classes — and
  puts a floor under what one object costs: a minimum billable size for the
  infrequent-access classes, a per-object overhead for the Glacier ones. A
  tree of many small files can cost more archived than it did in `STANDARD`.
- **Churn pays worse.** A file that changes gets re-uploaded as a fresh
  `STANDARD` object, while the archived copy it replaced is still billed for
  the rest of its minimum duration. Archive rules suit the parts of a backup
  that do not move.
- **`INTELLIGENT_TIERING` hides the problem.** Its archive tiers fail a
  `get_object` the same way, but a listing reports the object as
  `INTELLIGENT_TIERING` whatever tier it is actually in, so `verify` cannot
  warn about it. The failed pull is the first you hear of it.

**Keep the manifests out of any archive rule.** A manifest is downloaded by
every command, so archiving one takes the whole entry offline — not just its
restores:

```console
$ s3bak status demo
s3bak: An error occurred (InvalidObjectState) when calling the GetObject operation: The operation is not valid for the object's storage class
$ s3bak verify --all
s3bak: demo: An error occurred (InvalidObjectState) when calling the GetObject operation: The operation is not valid for the object's storage class
vault: OK (1 file record(s), 1 data object(s))
wsl.conf: OK (1 file record(s), 1 data object(s))
```

The layout makes this easy to arrange: an entry's data lives under
`backup/demo/`, while its manifest sits beside that directory at
`backup/demo-manifest.jsonl`. A lifecycle rule whose prefix filter is
`backup/demo/` therefore catches the data and nothing else. A rule filtered on
`backup/` alone catches every manifest in the backup.

### Bucket versioning

s3bak keeps one state, the one the last push left. Versioning is S3's own
answer to that, and it is the only thing between you and a push that backs up
a disaster: files encrypted by ransomware, a directory emptied by a bad
script, a `push --delete` answered `y` too quickly.

With versioning on, an overwrite keeps the previous version and a deletion
becomes a delete marker with the object still underneath it. s3bak neither
turns this on nor knows about it — it never asks for a version, so it always
reads and replaces the current one — and that is exactly why it works as a
safety net: nothing s3bak does can reach the older versions. The backup
credential from [Appendix A](appendix-a-aws-setup.md) reinforces it, since
`s3:DeleteObject` is enough to create a delete marker but not to destroy a
version.

The cost is that every version is stored and billed. On a backup, where each
push overwrites whatever changed, that adds up in proportion to how much of
the tree churns. Bound it with a lifecycle rule that expires noncurrent
versions after a retention you choose — 30 or 90 days is a common shape —
which turns versioning into "I can go back that far" rather than "I keep
everything forever".

Recovering from it is S3's job, not s3bak's. `aws s3api list-object-versions`
shows what an object has been, and putting an old one back is a copy of that
version over the current one (or, for something a delete marker hid, deleting
the marker). While doing it:

- **The manifest is an object too**, and its versions are how you undo a push
  that recorded the wrong thing. Restoring the data objects without the
  manifest that described them leaves the two disagreeing, which is what
  `verify` will tell you.
- **Settle with s3bak afterwards.** Once the objects are back where you want
  them, `verify` says whether the manifest agrees, and a push refreshes the
  manifest from the local tree.

### Abandoned multipart uploads

A large upload is a multipart upload, and one interrupted by something abrupt
enough can leave its parts in the bucket: invisible in a listing, billed as
storage. A lifecycle rule with `AbortIncompleteMultipartUpload` after a few
days cleans them up, which is S3's standard answer and the reason
[Appendix A](appendix-a-aws-setup.md)'s policy grants
`s3:AbortMultipartUpload`. [Recovery and
troubleshooting](08-recovery-troubleshooting.md) covers what leaves them
behind.

## Keeping the backup private

s3bak does not encrypt anything before uploading it. Objects are encrypted at
rest by S3 — SSE-S3 by default, and there is no unencrypted mode — but that
protects the disks in a datacentre, not the contents from anyone who can
authenticate. **Whoever can read the bucket can read your files**, so the
bucket and the credential are the entire boundary.

What that asks of you:

- **Leave Block Public Access on.** Nothing in s3bak needs the bucket to be
  reachable without credentials.
- **Keep the credential narrow and unshared.** The user and policy in
  [Appendix A](appendix-a-aws-setup.md) can write to one path of one bucket
  and do nothing else. A machine that only verifies can hold a credential
  narrower still, with no `PutObject` or `DeleteObject` at all.
- **Protect the credential file.** `~/.aws/credentials` is a plain file with a
  long-lived key in it; it deserves mode 600 and the same care as an SSH
  private key.
- **Decide what should not go up at all.** An entry's `excludes` keeps a path
  out of the backup entirely ([Configuration](03-configuration.md)), which is
  the simplest answer for a cache of tokens or a directory of scratch
  credentials.
- **Encrypt what must be secret before it becomes a backup.** A `pre_hook`
  that produces an encrypted archive — `age`, `gpg`, whatever you already
  trust — turns "a secret in the bucket" into "an encrypted file in the
  bucket", and s3bak backs up that file like any other. Remember that it will
  be a new object every time it is produced, so keep such an entry small.

SSE-KMS is the one server-side choice that changes how s3bak behaves: a
KMS-encrypted object's ETag is not a content digest, so `--checksum` cannot
compare against it and fails loudly rather than comparing nonsense ([How s3bak
detects changes](04-change-detection.md)).

## Next

- [Recovery and troubleshooting](08-recovery-troubleshooting.md) for what to
  do about anything the routine above reports.
- [Platform notes](09-platform-notes.md) for what changes on Windows, macOS,
  and WSL2, and for S3-compatible services.
- [Deleting safely](06-deleting-safely.md) before scheduling anything with
  `--delete` in it.
