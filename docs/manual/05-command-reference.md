# Command reference

Every command, every option, and what each one prints. The earlier chapters
explain how s3bak thinks; this one is for looking things up.

## The shape of a command line

```
s3bak <command> [options] [<entry|path>...]
```

Options and arguments may be interleaved, and an option that takes a value
accepts both spellings:

```console
$ s3bak status --mtime-window 2 bin
$ s3bak status --mtime-window=2 bin
```

`--` ends option parsing: everything after it is an argument, which is how you
name a path that begins with a dash. An option s3bak does not know stops the
command before anything happens:

```console
$ s3bak push --bogus demo
s3bak: unknown option: --bogus
```

The command line is parsed and checked **before** the configuration file is
read, and the configuration is read before any connection to S3 is made. A
mistyped option therefore reports the mistyped option, even on a machine whose
AWS profile is missing or whose network is down.

Two options stand outside the commands. `s3bak --version` prints the installed
version, and `s3bak --help` prints the command list; both go to standard
output and exit 0. Running s3bak with no command at all, or with one it does
not recognize, prints that same list on standard error and exits 1.

Each command has its own help, which is the shortest way to check an option
without leaving the terminal:

```console
$ s3bak status --help
Usage:
  s3bak status [options] <entry|path>...
  s3bak status [options] --all

Compare local files with the backup using metadata.
...
```

## Naming what to work on

Most commands take entries or paths. There are three ways to write one, and
s3bak tells them apart by looking for a path separator:

| What you type | How it is read |
| --- | --- |
| `bin` | the configured entry named `bin`, and nothing else |
| `bin/lib` | the path `lib` inside entry `bin`, relative to that entry's root |
| `/home/you/bin/lib`, `./lib`, `~/bin/lib` | a local path, normalized and then matched against the configured entries |

The middle form is anchored at the entry, not at your shell: `s3bak push
bin/lib` means the same thing from every working directory. A sub-path that
climbs out of its entry is refused rather than followed:

```console
$ s3bak push demo/../../escape
s3bak: sub path must stay inside entry demo: demo/../../escape
```

The third form is the convenient one — tab completion produces it — and it is
resolved by finding the entry whose root contains the path. When more than one
entry qualifies, **the longest root wins**, so a nested entry takes precedence
over the one it sits inside:

```python
entries = {
    "demo": {"path": "/home/you/demo"},
    "lib":  {"path": "/home/you/demo/lib"},
}
```

```console
$ s3bak push /home/you/demo/lib/util.sh
upload: /home/you/demo/lib/util.sh to s3://my-bucket/backup/lib/util.sh
Updating s3://my-bucket/backup/lib-manifest.jsonl
```

The file went into the backup of `lib`, not of `demo`. Two entries rooted at
the *same* directory would be a genuine tie, and s3bak refuses to guess:
`path is ambiguous between entries demo, lib: ...`.

A `~` is expanded from `HOME`, on every platform — including Windows, where
`HOME` is preferred over `USERPROFILE` when both are set.

When the name matches nothing, the message says which of the two lookups
failed:

```console
$ s3bak push nope
s3bak: no such entry: nope
$ s3bak push /etc/hostname
s3bak: no such entry for path: /etc/hostname
```

### `--all`

`--all` replaces the arguments with every configured entry, and cannot be
combined with them:

```console
$ s3bak push --all demo
s3bak: --all cannot be combined with explicit entries
```

Entries are taken in sorted name order, but they **run concurrently** — up to
`entry_concurrency` at a time — so the output of one can appear between the
lines of another. (A `--delete` run that is answering prompts is the
exception: it takes one entry at a time, so the questions stay in order.) What
does not vary is the exit status: it is the first non-zero one in that sorted
order, whichever entry happened to finish first.

### How many arguments each command takes

| Command | Arguments |
| --- | --- |
| `push`, `pull`, `status`, `verify` | one or more, or `--all` |
| `hook` | `pre` or `post`, then one or more entries, or `--all` |
| `diff`, `show` | exactly one |
| `ls-remote` | none, or one |
| `list` | none |

`hook` is the one command whose first argument is not an entry: it selects
which hook to run, and an entry may not carry a sub-path there.

### Arguments that conflict with each other

Naming several targets is allowed, but not every combination is coherent:

| Rejected | Message |
| --- | --- |
| the same entry twice in one `push`, `pull` or `hook` | `duplicate entry in push: demo (parallel push of the same entry is not supported)` |
| two sub-paths of one entry in `status` or `verify` | `conflicting sub paths for entry demo` |
| a `pull` whose targets restore into the same tree | `pull restore destinations overlap: demo (/home/you/demo) and lib (/home/you/demo/lib)` |

The last one applies to `pull --all` as well, so a configuration with a nested
entry is caught the first time you try to restore everything, rather than
halfway through.

## Options

| Option | Commands | What it does |
| --- | --- | --- |
| `--all` | `push`, `pull`, `status`, `verify`, `hook` | apply to every configured entry |
| `--dry-run` | `push`, `pull`, `hook` | report what would happen and change nothing |
| `--delete` | `push`, `pull` | remove what the source no longer has, after confirmation |
| `--delete` | `status` | list what `push --delete` would offer, removing nothing |
| `--yes` | `push`, `pull` | answer yes to every deletion confirmation |
| `--checksum` | `push`, `pull`, `verify` | compare content instead of size and modification time |
| `--mtime-window <seconds>` | `push`, `pull`, `status`, `verify` | override the configured tolerance for this run |
| `-o`, `--output <path>` | `pull` | restore one target to this exact path |
| `-v`, `--verbose` | all but `list` | show the requests and the details behind each line |
| `--color[=WHEN]` | `status`, `diff` | `auto` (the default), `always`, or `never`; bare `--color` means `always` |
| `--no-color` | `status`, `diff` | the same as `--color=never` |
| `--help` | every command | print that command's help and exit 0 |

An option a command does not accept is an error rather than something quietly
ignored, and the message names the commands that do accept it:

```console
$ s3bak status --checksum demo
s3bak: --checksum only applies to push, pull, and verify
```

That strictness matters most for `--dry-run`. A command that accepted it and
ignored it would carry out the real operation while its user was expecting a
rehearsal.

### Combinations that are refused

| What you asked for | Why it stops |
| --- | --- |
| `--yes` without `--delete` | `--yes` answers deletion confirmations; there are none to answer |
| `--checksum` with `--mtime-window` (except on `verify`) | a content comparison never looks at modification times |
| `--mtime-window` without `--checksum` on `verify` | there, the window only classifies content mismatches |
| `pull --all -o <path>` | one destination cannot hold every entry |
| `pull a b -o <path>` | likewise for several named targets |
| `-o ""` | an empty destination is never what was meant |

Each stops the command with exit 1 and a message that says as much:

```console
$ s3bak push --yes demo
s3bak: --yes requires --delete (it answers deletion confirmations)
$ s3bak verify --mtime-window 1 demo
s3bak: --mtime-window requires --checksum with verify (it classifies content mismatches)
```

### Values, and the values they refuse

An option that takes a value and is given none says so —
`s3bak: --mtime-window requires a value` — rather than swallowing whatever
came next.

`--mtime-window` takes a non-negative number of seconds, fractions included.
Anything else stops the run rather than being rounded into something usable:

```console
$ s3bak push --mtime-window abc demo
s3bak: --mtime-window requires a non-negative number of seconds (got 'abc')
$ s3bak push --mtime-window -1 demo
s3bak: --mtime-window must be >= 0 (got -1.0)
$ s3bak push --mtime-window 1e300 demo
s3bak: --mtime-window is too large to use (got 1e+300)
```

The last one is not pedantry: the window is held in nanoseconds internally, so
a value that large stops being a number s3bak can compare with.

`-o` refuses a value that begins with a dash, because that is nearly always a
forgotten argument rather than a filename:

```console
$ s3bak pull -o -weird demo
s3bak: -o requires a path value (use --output=<path> for a path starting with '-')
```

Naming such a path is still possible with the `=` form, which cannot be
mistaken for anything else: `s3bak pull --output=-weird demo`.

`--color` accepts only the three words:

```console
$ s3bak status --color=sometimes demo
s3bak: invalid --color value: sometimes (use auto|always|never)
```

Under `auto`, s3bak colorizes when standard output is a terminal and the
`NO_COLOR` environment variable is unset. An explicit `--color` or
`--no-color` overrides both.

### `--dry-run`

A dry run performs every step that does not change anything — the listings,
the manifest download and its validation, the comparison, the merge that
produces the new manifest — and suppresses only the mutations, the hooks and
the prompts. It makes exactly the requests a real run makes, never a cheaper
stand-in, so a rehearsal fails where the real command would fail, including on
a permission the real command would need.

Planned actions are marked:

```console
$ s3bak push --delete --dry-run demo
(dry-run) upload: /home/you/demo/new.sh to s3://my-bucket/backup/demo/new.sh
(dry-run) upload: /home/you/demo/notes.txt to s3://my-bucket/backup/demo/notes.txt
(dry-run) delete: s3://my-bucket/backup/demo/old-notes.txt
(dry-run) would update manifest: demo-manifest.jsonl
```

With `--delete`, every deletion candidate is listed instead of asked about.

### `-v`, `--verbose`

`-v` adds two things: the S3 requests, each echoed as a `+` line on standard
error before it is made, and the values behind whatever the command
summarized.

```console
$ s3bak push -v demo
+ (boto3) get_object s3://my-bucket/backup/demo-manifest.jsonl
+ (boto3-s3) sync /home/you/demo s3://my-bucket/backup/demo/
upload: /home/you/demo/new.sh to s3://my-bucket/backup/demo/new.sh
upload: /home/you/demo/notes.txt to s3://my-bucket/backup/demo/notes.txt
Updating s3://my-bucket/backup/demo-manifest.jsonl
+ (boto3) put_object s3://my-bucket/backup/demo-manifest.jsonl
```

### `--mtime-window` and `--checksum`

Both change what counts as a difference, and
[How s3bak detects changes](04-change-detection.md) is where they are
explained. In short: `--mtime-window` overrides the configured tolerance for
one run, and `--checksum` replaces the size-and-modification-time judgement
with one that reads content.

## `push`

```
s3bak push [options] <entry|path>...
s3bak push [options] --all
```

Copies the local state of each entry to S3 and records it in the manifest. A
push is what makes the backup; every other command reads what it left.

In order, one entry at a time: `pre_hook` runs, the manifest is downloaded,
the local tree is compared against it, the differences are uploaded, the new
manifest is written, and — only if the push did some work — `post_hook` runs.

```console
$ s3bak push demo
upload: /home/you/demo/new.sh to s3://my-bucket/backup/demo/new.sh
upload: /home/you/demo/notes.txt to s3://my-bucket/backup/demo/notes.txt
Updating s3://my-bucket/backup/demo-manifest.jsonl
```

A push that finds nothing to do prints nothing and exits 0.

Naming a sub-path — `s3bak push demo/lib` — pushes that subtree alone, and
nothing outside it is walked, compared or touched.

The entry's root has to be there. A push refuses to invent one, since the
alternative is a backup that quietly records an empty tree:

```console
$ s3bak push vault
s3bak: target does not exist: /home/you/vault
```

`push --delete` and `--yes` are how a backup loses things;
[Deleting safely](06-deleting-safely.md) covers them in full. `--checksum`
belongs to [How s3bak detects changes](04-change-detection.md).

## `pull`

```
s3bak pull [options] <entry|path>...
s3bak pull [options] --all
```

Restores an entry from S3 into its configured `path`, or into `-o` if you
name somewhere else. It transfers what differs, then applies the recorded
metadata:

```console
$ s3bak pull demo -o /tmp/restore
download: s3://my-bucket/backup/demo/notes.txt to /tmp/restore/notes.txt
download: s3://my-bucket/backup/demo/run.sh to /tmp/restore/run.sh
download: s3://my-bucket/backup/demo/lib/util.sh to /tmp/restore/lib/util.sh
644 /tmp/restore/lib/util.sh
755 /tmp/restore/lib
644 /tmp/restore/notes.txt
700 /tmp/restore/run.sh
755 /tmp/restore
```

The lines after the downloads are the metadata being applied: the recorded
permission bits, then the path they were set on. A directory is settled after
its contents, which is why `lib` follows what is inside it and the restore
root comes last — adding a file to a directory would otherwise leave that
directory's own modification time wrong. A path already carrying what
the record says is left alone and prints nothing, so a second pull of an
untouched tree is silent. A symlink is reported as the path it was placed at
and where it now points — `/tmp/restore/link -> notes.txt`.

A single-file entry moves as one object rather than through a directory sync,
and its line says which transfer path carried it:

```console
$ s3bak pull wsl.conf
download: s3://my-bucket/backup/wsl.conf to /etc/wsl.conf (boto3 get_object)
644 /etc/wsl.conf
```

`boto3-s3 cp` appears instead for an object at or above the multipart
threshold — 8 MiB unless the AWS configuration says otherwise — where the
download is split into parallel parts. `--dry-run` names the same lane.

`-o` takes one target and one destination, which is why it rules out `--all`
and several arguments at once. The destination is the path itself, not a
directory to put the entry inside: `pull wsl.conf -o /tmp/w.conf` writes that
file, and `pull demo -o /tmp/restore` fills that directory.

A pull never writes the manifest — only a push may — so a record that has gone
stale stays stale. Where the backup no longer holds the object a record names,
the pull says so, skips that one path and carries on:

```console
warning: no data object behind this record - skipped (a push retires the stale record): /home/you/demo/gone.txt
```

That is a warning, so the run finishes and exits 2. A single-file entry names
the object instead of the local path, since there is no tree to place it in.
[Recovery and troubleshooting](08-recovery-troubleshooting.md) covers what
leaves such a record behind, and `pull --delete` is in
[Deleting safely](06-deleting-safely.md).

## `status`

```
s3bak status [options] <entry|path>...
s3bak status [options] --all
```

Compares the local tree against the manifest and prints one line per
difference. It costs a single S3 request — the manifest download — and never
lists the bucket.

```console
$ s3bak status demo
M /home/you/demo	mtime
A /home/you/demo/new.sh
M /home/you/demo/notes.txt	size, mtime
M /home/you/demo/run.sh	mode
```

Each line is a letter, the path, and — after a tab, for `M` — the properties
that differed:

| Letter | Meaning |
| --- | --- |
| `M` | both sides have it; a push would update the backup |
| `A` | only local; a push would add it |
| `D` | only in the backup; `push --delete` would offer to remove it. Printed by `status --delete` alone |

| Tag | What differs |
| --- | --- |
| `size` | the size of a regular file |
| `mode` | the permission bits |
| `mtime` | the modification time, beyond the tolerance |
| `link` | where a symlink points |
| `type` | the kind of thing at that path |

A regular file prints its tags as `size, mode, mtime` and a symlink as
`link, mtime`. A `type` difference stands alone, since nothing else about two
different kinds of thing is worth comparing.

`-v` prints the values under each line, indented, and adds the request trace:

```console
$ s3bak status -v demo
+ (boto3) get_object s3://my-bucket/backup/demo-manifest.jsonl
M /home/you/demo	mtime
      mtime: remote=2026-08-14 11:19:59 < local=2026-08-14 11:20:00 (+1s)
A /home/you/demo/new.sh
M /home/you/demo/notes.txt	size, mtime
      size: remote=34 < local=35 (+1 bytes)
      mtime: remote=2026-08-14 11:19:59 < local=2026-08-14 11:20:00 (+1s)
M /home/you/demo/run.sh	mode
      mode: remote=755 local=700
```

`remote` is what the manifest recorded and `local` is what is there now; the
`<` and `>` point at the larger or later of the two, and colour marks the same
side green. A `type` line names the two kinds — `type: remote=symlink
local=regular file`. Larger differences also print a readable form of
themselves: `(+3145728 bytes (+3.00 MB))` for a size, `(+2d 3h)` for a time,
and fractional seconds when the drift is under a second.

Silence means everything matched. What the letters mean, and why a plain
`status` never prints `D`, is [How s3bak detects
changes](04-change-detection.md).

Three situations produce a warning rather than a letter, and each makes the
run exit 2:

```console
$ s3bak status vault
warning: local path does not exist (a push would refuse): /home/you/vault
```

A missing entry root is the one state a plain `status` cannot describe as a
push preview, because the push would not run at all. The other two are a path
s3bak cannot look at (`warning: cannot access <path>: <error>`) and a
sub-path reached through a symlinked parent, which is reported instead of
compared:

```console
$ s3bak status demo/lib/util.sh
warning: demo/lib/util.sh: reached through a symlinked parent; not compared
```

Adding `--delete` to either of the two changes what follows the warning. There
is nothing local that s3bak is willing to compare, so every record in range
prints as `D` — the backup a `push --delete` would offer to retire:

```console
$ s3bak status --delete demo
warning: local path does not exist (a push would refuse): /home/you/demo
D /home/you/demo
D /home/you/demo/link
D /home/you/demo/notes.txt
```

## `verify`

```
s3bak verify [options] <entry|path>...
s3bak verify [options] --all
```

Reads the manifest and the stored objects and checks that they agree. It
changes nothing, needs no write permission, and is the only command that looks
for damage done to the backup from outside.

Every recorded regular file must have its data object, of the recorded size
and in a storage class a restore can fetch; directories, symlinks and special
files must have no object at all. Objects the manifest does not mention are
reported, and so are the `/`-terminated "folder" objects that some tools
create.

```console
$ s3bak verify demo
demo: OK (5 file record(s), 5 data object(s))
```

Every run ends with one summary line per entry — the tallies double as a
heartbeat for a scheduled run. Findings come first, and change the shape of
that line:

```console
$ s3bak verify demo
warning: demo: unrecorded object: s3://my-bucket/backup/demo/stray.txt (not in the manifest; push --delete decides its fate)
demo: 0 error(s), 1 warning(s) (5 file record(s), 6 data object(s))
```

Findings come in three severities. **Errors** mean the backup does not hold
what the manifest promises, and exit 1. **Warnings** mean something sits
outside the backup — an unrecorded object, an archived storage class, records
left under an exclude — and exit 2. **Pending changes** are informational, do
not affect the exit status, and are printed as `pending change:`.

`--all` adds a sweep of the prefix's top level after the entries, warning
about anything no configured entry accounts for:

```console
$ s3bak verify --all
vault: OK (1 file record(s), 1 data object(s))
wsl.conf: OK (1 file record(s), 1 data object(s))
warning: demo: unrecorded object: s3://my-bucket/backup/demo/stray.txt (not in the manifest; push --delete decides its fate)
demo: 0 error(s), 1 warning(s) (5 file record(s), 6 data object(s))
warning: stale manifest (no configured entry): s3://my-bucket/backup/gone-manifest.jsonl
warning: top-level object outside any configured entry: s3://my-bucket/backup/leftover.txt
```

`--checksum` adds a content comparison against the stored ETags, which is what
finds an edit that size and modification time cannot see; `--mtime-window`
exists here only to classify those mismatches, which is why it requires
`--checksum`. Both are in [How s3bak detects
changes](04-change-detection.md), and what to do about each finding is in
[Recovery and troubleshooting](08-recovery-troubleshooting.md).

## `hook`

```
s3bak hook <pre|post> [options] <entry>...
s3bak hook <pre|post> [options] --all
```

Runs one configured hook on its own, outside any push — to re-run an off-site
copy after the far side changed, or to test a dump script before trusting a
backup to it.

```console
$ s3bak hook post vault
offsite copy done
```

The hook runs exactly as a push runs it: an argument vector, no shell, standard
input detached, the same treatment of its exit status. `S3BAK_JOURNAL` is
unset, since there was no push and therefore no journal, which a hook reads as
"no per-file detail; assume anything may have changed"
([Operating s3bak](07-operating.md)). The command needs no S3 access at all.

`pre` or `post` has to come first, and the rest of the arguments are whole
entries:

```console
$ s3bak hook vault
s3bak: hook requires 'pre' or 'post' as its first argument, got 'vault'
$ s3bak hook post demo/lib
s3bak: hook runs per entry; a sub path is not allowed: demo/lib
```

`s3bak hook --help` is the exception to the first rule: it prints the help
that explains the syntax rather than complaining about it.

Naming an entry is an instruction, so an entry without that hook is an error:

```console
$ s3bak hook pre vault
s3bak: vault: no pre_hook configured
```

That is deliberate. The configuration ignores keys it does not recognize, so a
`post_hok:` typo produces an entry with no hook, and this is the message that
reveals it. `--all` reads differently — "run every configured hook of this
kind" — and skips the entries that have none:

```console
$ s3bak hook post --all -v
skipped (no post_hook): demo
skipped (no post_hook): wsl.conf
+ post_hook: ['/home/you/bin/offsite']
offsite copy done
```

It fails only when no entry configures that hook at all, since an instruction
that turns out to be a no-op in full should not read as success:

```console
$ s3bak hook pre --all
s3bak: no entry configures a pre_hook
```

`--dry-run` prints the command line instead of running it:

```console
$ s3bak hook post vault --dry-run
(dry-run) would run post_hook: ['/home/you/bin/offsite']
```

## `diff`

```
s3bak diff [options] <entry|path>
```

Shows what actually differs inside the files, by running the system's `diff`
over each recorded file and its local counterpart. `a/` is the backup and
`b/` is local:

```console
$ s3bak diff demo/notes.txt
--- a/demo/notes.txt
+++ b/demo/notes.txt
@@ -1,3 +1,3 @@
 first line
 second line
-third line
+THIRD line!
```

Pointed at a whole entry, it covers every recorded file, including the ones
that exist on only one side:

```console
$ s3bak diff demo
--- a/new.sh
+++ b/new.sh
@@ -0,0 +1 @@
+new
--- a/old-notes.txt
+++ b/old-notes.txt
@@ -1 +0,0 @@
-old
```

This is an investigation tool, not a routine check. It **downloads every
recorded regular file** — one at a time, so the disk holds at most one, but
the transfer is the whole entry. `status` is the cheap question; `diff` is the
one you ask about a particular file once `status` has pointed at it.

Differences are not an error, but they do show in the exit status: `diff`
exits 1 when it found any and 0 when the two sides matched. It needs a `diff`
program on `PATH`, and colorizes through it when that program supports
`--color`.

## `show`

```
s3bak show [options] <entry|path>
```

Prints one backed-up file to standard output, straight from S3, without
touching the local copy. It is the quickest way to look at what the backup
actually holds:

```console
$ s3bak show wsl.conf
[boot]
systemd=true
```

The output is the stored bytes and nothing else, so it pipes and redirects as
you would expect. Only a regular file has stored content, and `show` says
which of the possible reasons stopped it:

```console
$ s3bak show demo
s3bak: only a regular file can be shown, not a directory: demo
$ s3bak show demo/link
s3bak: only a regular file can be shown, not a symlink: demo/link
$ s3bak show demo/nope.txt
s3bak: not found on S3: demo/nope.txt
```

A path the manifest records as a regular file whose object is nonetheless
gone gets the same reading a pull gives it — `no data object behind this
record (a push retires the stale record)`. Each of these exits 1.

`show` is also the one command that keeps working while a manifest is
damaged: it fetches the object first and consults the manifest only to
explain a miss, so a backup whose manifest needs repairing can still be read
file by file. The same order means an object the manifest does not record —
the kind `verify` warns about — is printed rather than refused, which is how
you look at one before deciding its fate.

## `list`

```
s3bak list
```

Prints the configured entries and their local paths, sorted by name. It reads
the configuration and stops there — no S3 request, no credentials, no network:

```console
$ s3bak list
demo                 /home/you/demo
vault                /home/you/vault
wsl.conf             /etc/wsl.conf
```

## `ls-remote`

```
s3bak ls-remote [options] [entry|path]
```

The counterpart to `list`: what is on S3 rather than what is configured. With
no argument it names every entry the bucket holds a manifest for, which
includes entries the configuration no longer mentions:

```console
$ s3bak ls-remote
demo
vault
wsl.conf
```

With an entry, it prints that entry's manifest — mode, owner, group, size,
modification time, and the entry-relative path, with a symlink's target after
an arrow:

```console
$ s3bak ls-remote demo
40755  you      you                2026-08-14 11:21:06  .
120777 you      you                2026-08-14 11:21:06  ./link -> notes.txt
100644 you      you             8  2026-08-14 11:21:06  ./notes.txt
40755  you      you                2026-08-14 11:21:06  ./sub
100644 you      you             2  2026-08-14 11:21:06  ./sub/a.txt
```

A sub-path narrows that to one subtree. The mode is the full octal value, so
the leading digits give the kind: `100644` a regular file, `40755` a
directory, `120777` a symlink. Owner and group are recorded for reporting
only — a pull never applies them.

Listing an entry requires it to be *configured*, since that is where the
manifest's name comes from. An entry that exists only on S3 shows up in the
argument-less listing and can be inspected no further; a `verify --all` names
it as a stale manifest.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success, with no warnings |
| 1 | a usage, configuration, local I/O, S3, or manifest error |
| 2 | the work finished, but something warned |
| 3 and up | a hook's own exit status, passed through |
| 130 | interrupted (`Ctrl-C`) |
| 141 | the output pipe was closed |

Exit 2 is the one worth wiring into a scheduled run. It means s3bak did what
it was asked and something is nonetheless not right — an unreadable directory
skipped during a push, a stale record skipped during a pull, anything `verify`
warned about. The work was done; a human should still look.

A few commands read these codes slightly differently:

- **`diff`** exits 1 when it found differences, which is the convention its
  namesake follows.
- **`verify`** exits 1 if it reported any error and 2 if it only warned.
- **`--delete`** answered "no" to everything is a successful run (0), because
  "no" is an answer. Aborting the prompt with `q` is a failure (1), and so is
  declining a whole-subtree deletion, since in both cases the command did not
  do what it was asked. [Deleting safely](06-deleting-safely.md) has the
  detail.
- **a failing hook** hands back its own status, with two adjustments: an exit
  of 2 becomes 1, because 2 is reserved for s3bak's warnings, and a hook
  killed by signal *N* becomes 128+*N*.

When a command runs several entries, the exit status is the first non-zero one
in the order the entries were named — sorted name order under `--all` — rather
than whichever entry happened to fail first. Repeating a run therefore gives a
repeatable answer.

## Next

- [Deleting safely](06-deleting-safely.md) for `--delete`, `--yes`, and the
  confirmation prompt.
- [Operating s3bak](07-operating.md) for putting these commands into a
  routine.
- [Recovery and troubleshooting](08-recovery-troubleshooting.md) for what to
  do when one of them reports something.
