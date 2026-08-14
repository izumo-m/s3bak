# Recovery and troubleshooting

Two kinds of trouble bring you here: a command that stopped in the middle, and
a message someone has to act on. This chapter covers both — what an
interruption can leave behind, what a hard kill leaves that s3bak cannot clean
up itself, and what to do about every finding `verify` reports.

## The rule behind almost every answer

s3bak is built so that an interrupted run falls back rather than forward. Two
properties do the work:

- **Every S3 change completes before the manifest describing it is
  published.** Uploads land first, deletions finish first, and only then is
  the manifest rewritten.
- **A manifest is never half-written.** It is streamed to a local temporary
  file, validated with the same rules a read applies, and uploaded as one
  object. Either the old manifest is there or the complete new one is.

So the worst state an interruption can leave is *some* of the uploads and
deletions done, described by the *previous* manifest. Nothing is corrupted,
and the backup is never less restorable than it was before the run started.

Which is why the answer is almost always the same: **run the command again.**
The re-run compares from scratch, sees what the manifest does not know about,
and settles it.

| Interrupted | What is left | What settles it |
| --- | --- | --- |
| a push, anywhere before the manifest write | uploads that landed, unrecorded | run `push` again |
| a push whose deletions had not all run | the records and objects it never reached | run `push --delete` again |
| a push after a deletion landed | records whose objects are gone | any `push` — it retires them |
| a push during `post_hook` | a complete backup, a half-done hook | `s3bak hook post <entry>` |
| a pull | a partly updated local tree | run `pull` again |
| a pull that had removed some extras | the rest of the extras | run `pull --delete` again |

The exception is a *hard* kill — `kill -9`, a power loss — which runs none of
s3bak's cleanup. The backup on S3 is still consistent, but a few things are
left on the local disk for you to deal with; they are listed further down.

## An interrupted push

The residue of a push that stopped before its manifest write is objects the
manifest does not mention. `verify` is what sees them:

```console
$ s3bak verify demo
warning: demo: unrecorded object: s3://my-bucket/backup/demo/late.txt (not in the manifest; push --delete decides its fate)
demo: 0 error(s), 1 warning(s) (3 file record(s), 4 data object(s))
```

If the local file is still there — the ordinary case, since the push uploaded
it from somewhere — the next plain push adopts the pair and the finding goes
away:

```console
$ s3bak push demo
upload: /home/you/demo/late.txt to s3://my-bucket/backup/demo/late.txt
Updating s3://my-bucket/backup/demo-manifest.jsonl
$ s3bak verify demo
demo: OK (4 file record(s), 4 data object(s))
```

It re-uploads rather than adopting the bytes where they lie, because the
manifest records what the push *saw*, and it has not seen this file yet.

An unrecorded object whose local file is **gone** cannot be adopted — there is
no local file to record — so it stays until you retire it with
`push --delete`. [Deleting safely](06-deleting-safely.md) covers that
conversation, including why the prompt keeps asking.

### A record whose object is gone

The mirror image is an interrupted deletion: the object went, the manifest
still records it. `verify` calls that an error, because the backup no longer
holds what the manifest promises:

```console
$ s3bak verify demo
s3bak: demo: missing data object: s3://my-bucket/backup/demo/todo.txt (pull cannot restore it)
demo: 1 error(s), 0 warning(s) (4 file record(s), 3 data object(s))
```

A pull meeting that record says so, skips it, and restores everything else:

```console
$ s3bak pull demo
warning: no data object behind this record - skipped (a push retires the stale record): /home/you/demo/todo.txt
755 /home/you/demo
```

What settles it is a plain push, and which way depends on the local file. If
the file is gone too, the record backs up nothing, so any push retires it — no
`--delete` and no question, because dropping a record that describes nothing
is repair rather than deletion:

```console
$ s3bak push demo
Updating s3://my-bucket/backup/demo-manifest.jsonl
$ s3bak verify demo
demo: OK (3 file record(s), 3 data object(s))
```

If the file is still there, the same push uploads it again instead, and the
pair is whole.

### What a re-run cannot do for you

**Deletions that had not run yet still need `push --delete`.** A record whose
object is still on S3 is a real backup, and only a confirmed deletion may drop
it. Re-running a plain push settles the uploads and leaves those alone.

**An interrupted `post_hook` does not run again.** It fires after the manifest
is published, so the backup is complete and only the hook's own effects — an
off-site copy, a notification — are half-done. The next push has nothing to
do, so it runs no hook. Re-run it by hand with `s3bak hook post <entry>`,
remembering that `S3BAK_JOURNAL` described that push alone and is gone
([Operating s3bak](07-operating.md)).

**An aborted `--delete` says what it left unsettled.** Answering `q`, like any
other stop, leaves the manifest unwritten; the message names the entry and the
remedy, and [Deleting safely](06-deleting-safely.md) explains what may have
already happened.

## An interrupted pull

A pull never writes to S3, so an interruption can only leave the local tree
partly updated. Every record is judged again from scratch on the next run, so
a plain `pull` converges whatever was missed.

| Interrupted during | What is left locally | What settles it |
| --- | --- | --- |
| the manifest download | nothing touched | `pull` |
| the downloads | fully written files only — a partial download is a uniquely named sibling, never the destination name | `pull`; the leftover shows up as an extra |
| a staged restore, before the swap | the old tree intact; only the stage holds new data | `pull`, then remove the stage |
| the metadata pass | some permissions and times applied | `pull` — it only touches what differs |
| removing extras (`--delete`) | some extras gone, the rest present | `pull` re-settles metadata; `pull --delete` removes the rest |

The download property is worth stating plainly: **a file is either its old
self or its new self**. s3bak writes every download into a temporary sibling
and renames it into place, so an interrupted transfer never leaves a
half-written file under the real name.

### After a power loss, pull again with `--checksum`

That rename is atomic against a process that dies, but s3bak does not `fsync`
the file, so it is not atomic against the machine losing power. A write-back
filesystem can keep the rename and the applied timestamp while losing the
contents — and since size and modification time then still match the record,
a later `pull` and `status` both call it a match.

```sh
s3bak pull --checksum demo
```

That compares content instead, and re-downloads anything that does not match.
`verify` does not help here: it checks the manifest against S3, never against
the local tree.

## Leftovers a hard kill can leave

None of these damages the backup. They are files nobody removed because the
process never got to.

| Leftover | What it is | What to do |
| --- | --- | --- |
| `.s3bak-download-*` | a partial download, in the destination's own directory | delete it; `pull` again |
| `<name>.s3bak-new-*` | a symlink being swapped into place | delete it; `pull` again |
| `<name>.s3bak-old-*` | a directory moved aside so a symlink could take its name | check it, then delete it; `pull` again first if the name is missing |
| `<name>.s3bak-stage-*` | a staged restore root, holding the new tree and the old one it was replacing | `pull` again to restore the path, then inspect and remove the stage |
| an abandoned multipart upload | the parts of a large upload nobody aborted — invisible in a listing, billed as storage | a bucket lifecycle rule with `AbortIncompleteMultipartUpload` ([Operating s3bak](07-operating.md)) |
| temp files in the system temp directory | manifests, journals and diff staging a run would have unlinked | delete them; nothing reads them back |

The ones that sit inside a restored tree show up as extras — `status` reports
them with `A` and `pull --delete` offers to remove them — with one exception:
a leaf sub-path restored with `-o` does not walk its own siblings, so a
leftover next to it has to go by hand.

The stage directory is the one to look inside before deleting. When a pull
must replace the restore root itself, it downloads into `<stage>/new` and
moves the old root to `<stage>/replaced`; a kill in between can leave the
configured path missing while the old tree sits there.

A pull that fails *after* that swap — during the metadata pass, say — keeps
the old root rather than cleaning it away, and says where it went:

```console
s3bak: demo: pull failed after replacing /home/you/demo; the previous /home/you/demo is preserved at /home/you/demo.s3bak-stage-ab12cd/replaced
```

That is an ordinary failure rather than a hard kill, so the message is there
to read: fix what failed, pull again, and remove the stage once you are
satisfied with what came back.

## What `verify` found

`verify` reports in three severities, and the exit status follows the worst
one:

- **Errors (exit 1)** — the backup does not hold what the manifest promises.
- **Warnings (exit 2)** — the backup itself is not in question; something sits
  outside it, or cannot be checked.
- **Pending changes (exit 0)** — informational, and only under `--checksum`.

| Finding | What happened | What settles it |
| --- | --- | --- |
| `missing data object` | a recorded file has no object | `push` — it re-uploads the file, or retires the record if the file is gone |
| `size mismatch` | the object is not the size the record says | `push` |
| `type conflict` | an object sits where the manifest records a directory, symlink, or special file | `push --delete` |
| `unrecorded object` | an object the manifest does not mention | `push` if its local file exists; otherwise `push --delete` |
| `unrestorable` | a non-directory and a directory are recorded at one path | `push --delete` |
| `folder object` | a `/`-terminated key, which s3bak never writes | `aws s3 rm` |
| `folder object with data` | the same, but carrying bytes that can never restore | `aws s3 rm`, after retrieving the bytes if they matter |
| `content differs but size+mtime match` | the blind spot: a local edit no ordinary push will ever notice | `push --checksum` |
| `recorded path(s) under excludes` | records left from before an exclude was added | `push --delete` |
| `archived storage class` | the object is in Glacier or Deep Archive and cannot be fetched yet | restore it in S3, or change the lifecycle rule ([Operating s3bak](07-operating.md)) |
| `cannot read local file for --checksum` | the content check could not read a local file | fix the permissions, or ignore it if the path is meant to be unreadable |
| `no backup on S3` | the entry was never pushed | `push` |
| `data objects exist but no manifest records them` | a push that never wrote its manifest, or a manifest since deleted | `push` |
| `pending change` | an ordinary local edit, not yet pushed | `push`, or nothing |

The rest of this section is the findings that need more than a line.

### `status` is silent about most of them

A finding from `verify` usually does **not** show up in `status`, and that is
not a bug in either. `status` compares the local tree against the manifest;
`verify` compares the manifest against S3. When the local tree and the
manifest agree, `status` has nothing to say even though the backup is broken:

```console
$ s3bak verify demo
s3bak: demo: missing data object: s3://my-bucket/backup/demo/notes.txt (pull cannot restore it)
demo: 1 error(s), 0 warning(s) (3 file record(s), 2 data object(s))
$ s3bak status demo
$
```

The same goes for a size mismatch. This is the whole reason `verify` exists,
and the reason it belongs in a routine rather than in your memory.

### `unrestorable`: a file and a directory at one path

The manifest can end up recording both `./lib` as a file and things under
`./lib/`, if a local directory is replaced by a file of the same name and then
pushed. The push itself says so and exits 2:

```console
$ s3bak push demo
upload: /home/you/demo/lib to s3://my-bucket/backup/demo/lib
warning: manifest keeps records under non-directory ./lib; pull cannot restore them (push --delete prunes them)
Updating s3://my-bucket/backup/demo-manifest.jsonl
```

Nothing is lost — both the file and the old subtree are backed up — but the
entry cannot be restored as a tree, so a pull refuses the whole entry rather
than restoring half of it:

```console
$ s3bak verify demo
s3bak: demo: unrestorable: a non-directory and a directory (or files under it) are both recorded at ./lib (pull cannot restore it; push --delete prunes it)
demo: 1 error(s), 0 warning(s) (4 file record(s), 4 data object(s))
$ s3bak pull demo -o /tmp/drill
s3bak: demo: unrestorable manifest - a non-directory and a directory (or files under it) are both recorded at ./lib; run push --delete to prune the stale records
```

`push --delete` retires the shadowed records and the objects under them:

```console
$ s3bak push --delete --yes demo
delete record: s3://my-bucket/backup/demo/lib/
delete: s3://my-bucket/backup/demo/lib/util.sh
Updating s3://my-bucket/backup/demo-manifest.jsonl
$ s3bak verify demo
demo: OK (3 file record(s), 3 data object(s))
```

If the shadowed subtree still matters, pull it out before pruning: the objects
are ordinary S3 objects, so `s3bak show demo/lib/util.sh` or the AWS CLI can
fetch them while the records still name them.

### `type conflict`

A regular file replaced locally by a symlink or a special file leaves its old
data object behind: the push records the new, objectless kind, but retiring
the object is a deletion and waits to be confirmed.

```console
s3bak: demo: type conflict: s3://my-bucket/backup/demo/draft.txt (manifest records a symlink, but a data object exists at its key)
```

`push --delete` offers it, flagged `(not in manifest)`
([Deleting safely](06-deleting-safely.md)). Until then the object is harmless
except that it collides with a restore of that path.

### `folder object`

Some tools — the S3 console among them — represent a folder with a
`/`-terminated key. s3bak never writes one, and it can never restore to a
local path.

```console
$ s3bak verify demo
warning: demo: folder object: s3://my-bucket/backup/demo/lib/ (not created by s3bak; remove with aws s3 rm)
s3bak: demo: folder object with data: s3://my-bucket/backup/demo/other/ (5 bytes; a '/'-terminated key cannot restore to a local path)
demo: 1 error(s), 1 warning(s) (3 file record(s), 5 data object(s))
```

A zero-byte one is only noise: the transfer skips it, and removing it is
housekeeping. One carrying bytes is an error, because those bytes have no path
to come back to; whatever put them there is what you are looking for.

### `content differs but size+mtime match`

The one finding that says something no other command can see: a local file
whose content changed without its size or modification time changing, so the
ordinary comparison will skip it forever.

```console
$ s3bak status demo
$ s3bak verify --checksum demo
s3bak: demo: content differs but size+mtime match: /home/you/demo/notes.txt (push will not upload it; use push --checksum)
demo: 1 error(s), 0 warning(s) (3 file record(s), 3 data object(s))
```

`push --checksum` compares content on the push side too, so it uploads the
file and the backup catches up:

```console
$ s3bak push --checksum demo
upload: /home/you/demo/notes.txt to s3://my-bucket/backup/demo/notes.txt
Updating s3://my-bucket/backup/demo-manifest.jsonl
```

Its quieter sibling is a **pending change**: the content differs *and* the
stat drifted, so this is an ordinary edit the next push will handle. It is
printed for completeness and leaves the exit status alone:

```console
$ s3bak verify --checksum demo
demo: pending change: /home/you/demo/notes.txt (a push will upload it)
demo: OK (3 file record(s), 3 data object(s), 1 pending change(s))
```

[How s3bak detects changes](04-change-detection.md) explains why the blind
spot exists and what keeps it short-lived.

### `no backup on S3`, and its opposite

An entry with no manifest is one of two very different situations, and
`verify` tells them apart. Nothing on S3 at all means the entry has simply
never been pushed:

```console
$ s3bak verify --all
s3bak: demo: no backup on S3 (entry never pushed)
demo: 1 error(s), 0 warning(s) (0 file record(s), 0 data object(s))
```

Objects without a manifest mean a push uploaded data and never got to write
the manifest — or that the manifest was deleted since:

```console
$ s3bak verify demo
s3bak: demo: data objects exist but no manifest records them (interrupted push? a push records them)
demo: 1 error(s), 0 warning(s) (0 file record(s), 3 data object(s))
```

A push settles both, treating everything it finds locally as new.

### The `--all` sweep

`verify --all` finishes by listing the top level of the prefix and warning
about anything no configured entry accounts for:

```console
$ s3bak verify --all
wsl.conf: OK (1 file record(s), 1 data object(s))
demo: OK (3 file record(s), 3 data object(s))
warning: stale manifest (no configured entry): s3://my-bucket/backup/gone-manifest.jsonl
warning: top-level object outside any configured entry: s3://my-bucket/backup/leftover.txt
warning: data tree without a manifest or configured entry: s3://my-bucket/backup/orphan/
```

None of these breaks a configured entry, and s3bak will not touch them: an
entry it does not know about is not its business. They are for you to
recognize.

- **A stale manifest** is the backup of an entry the configuration no longer
  has. It is still a complete backup — put the entry back to restore from it,
  or remove the manifest and its tree with the AWS CLI once you are sure.
- **A top-level object** is something that arrived under the prefix from
  outside s3bak.
- **A data tree without a manifest** is either the same, or an entry whose
  first push never finished.

## A damaged manifest

Every command validates the manifest before it does anything else, so damage
is caught while the backup is still intact rather than acted on:

```console
$ s3bak status demo
s3bak: not an s3bak v3 manifest (bad or missing header line)
$ s3bak push demo
s3bak: not an s3bak v3 manifest (bad or missing header line)
$ s3bak pull demo
s3bak: not an s3bak v3 manifest (bad or missing header line)
```

That includes `push`, which is the trap: the command that would rewrite the
manifest refuses to run against one it cannot read.

The data objects are untouched, and `show` does not need the manifest at all —
it streams the object at the key you name, which makes it the way to get a
file out while the entry is in this state:

```console
$ s3bak show demo/notes.txt
notes, edited normally
```

The AWS CLI works just as well, since data objects are stored as themselves
([Introduction](01-introduction.md)).

To repair it, get rid of the unreadable manifest and let a push write a new
one. If the bucket has versioning, the previous version of the manifest object
is the cheaper fix ([Operating s3bak](07-operating.md)); otherwise remove the
object:

```sh
aws s3 rm s3://my-bucket/backup/demo-manifest.jsonl
```

`verify` then reports the entry as data without a manifest, and the push
records it all again:

```console
$ s3bak verify demo
s3bak: demo: data objects exist but no manifest records them (interrupted push? a push records them)
demo: 1 error(s), 0 warning(s) (0 file record(s), 3 data object(s))
$ s3bak push demo
upload: /home/you/demo/late.txt to s3://my-bucket/backup/demo/late.txt
upload: /home/you/demo/lib to s3://my-bucket/backup/demo/lib
upload: /home/you/demo/notes.txt to s3://my-bucket/backup/demo/notes.txt
Updating s3://my-bucket/backup/demo-manifest.jsonl
$ s3bak verify demo
demo: OK (3 file record(s), 3 data object(s))
```

Note what that push cost: with no manifest to compare against, every file was
uploaded again. Note also what it recorded — the *local* tree as it is now. A
file that was in the backup and has since been deleted locally is not in the
new manifest, and its object becomes an unrecorded object rather than a
backup. If that matters, get the old manifest back from a bucket version
instead.

## Starting from nothing

To restore onto a machine that has nothing, s3bak needs a configuration file:
it is the only thing that says which profile, which bucket, and which entry
goes where. If your configuration file is not itself in the backup, write a
minimal one — a profile, a prefix, and any one entry to satisfy the
requirement that there be one:

```python
profile = "s3bak"
prefix = "s3://my-bucket/backup"

entries = {
    "placeholder": {"path": "/tmp/placeholder"},
}
```

`ls-remote` then inventories the bucket, naming every entry it holds a
manifest for ([Command reference](05-command-reference.md)):

```console
$ s3bak ls-remote
demo
wsl.conf
```

Write those entries into the configuration with the paths you want them
restored to — they need not be the paths they came from — and pull:

```console
$ s3bak pull --all
download: s3://my-bucket/backup/wsl.conf to /home/you/etc/wsl.conf (boto3 get_object)
644 /home/you/etc/wsl.conf
download: s3://my-bucket/backup/demo/late.txt to /home/you/demo/late.txt
download: s3://my-bucket/backup/demo/lib to /home/you/demo/lib
download: s3://my-bucket/backup/demo/notes.txt to /home/you/demo/notes.txt
644 /home/you/demo/late.txt
644 /home/you/demo/lib
644 /home/you/demo/notes.txt
755 /home/you/demo
$ s3bak status --all
$
```

A silent `status` afterwards is the confirmation: what is on disk now matches
what the backup recorded.

What does not come back is worth knowing before you need it — owner and group,
hard links, ACLs and extended attributes, and special files that do not
already exist. [Introduction](01-introduction.md) lists the limits in full,
and [Platform notes](09-platform-notes.md) covers restoring onto a different
operating system than the one that pushed.

Keeping the configuration file itself inside an entry is the way to make this
step unnecessary next time.

## When a command will not run at all

```console
$ s3bak status nosuch
s3bak: no such entry: nosuch
```

The name has to be one of the keys in `entries` — `s3bak list` prints them,
along with the paths they point at.

```console
$ s3bak push demo
s3bak: target does not exist: /home/you/demo
$ s3bak status demo
warning: local path does not exist (a push would refuse): /home/you/demo
```

A push refuses to back up a path that is not there, since the alternative is
recording an empty tree over a good backup. `status` says the same thing as a
warning, because it is only describing.

```console
$ s3bak list
s3bak: config file not found: /home/you/.config/s3bak/config.py

Create it with contents like:

  profile = "default"
  prefix  = "s3://my-bucket/backup"

  entries = {
      "home-docs": {"path": "/home/user/Documents"},
  }
```

Every command reads the configuration first, so a mistake in it stops whatever
you happened to run. [Configuration](03-configuration.md) covers the messages
for a file that exists but is wrong.

Anything s3bak could not do on S3 is reported in S3's own words, because that
is the honest thing to repeat:

```console
$ s3bak status demo
s3bak: An error occurred (NoSuchBucket) when calling the GetObject operation: The specified bucket does not exist
```

The ones worth recognizing:

- **`NoSuchBucket`** — the `prefix` names a bucket that does not exist, or the
  profile points at a different region than the bucket. Check both.
- **`AccessDenied`** — the credential reached S3 and was refused. The policy
  in [Appendix A](appendix-a-aws-setup.md) lists what each command needs; a
  `push --delete` failing where a `push` succeeds means `s3:DeleteObject` is
  missing.
- **`InvalidObjectState`** — the object is archived
  ([Operating s3bak](07-operating.md)).

Two more come from below S3, when the request was never made at all:

```console
$ s3bak status demo
s3bak: The config profile (s3bak) could not be found
$ s3bak status demo
s3bak: Unable to locate credentials
```

The first means the `profile` named in the configuration is not in the
`~/.aws/config` being read; the second means it is, but carries no key. Both
are the usual reason a backup works when you type it and fails from a
scheduler — a scheduled job reads the AWS configuration of the user it runs
as ([Operating s3bak](07-operating.md)).

For anything credential-shaped, the AWS CLI is the better instrument:
`aws sts get-caller-identity --profile <profile>` says who you are, and
`aws s3 ls s3://my-bucket/backup/ --profile <profile>` says whether that
identity can reach the backup. s3bak can only repeat what S3 told it; the CLI
can ask on its own behalf.

## Next

- [Platform notes](09-platform-notes.md) for the differences between operating
  systems and filesystems, including S3-compatible services.
- [Operating s3bak](07-operating.md) for the routine that finds these problems
  while they are small.
- [Deleting safely](06-deleting-safely.md) for the `--delete` runs several
  findings above ask for.
