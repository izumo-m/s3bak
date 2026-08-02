# Configuration

One file describes everything s3bak backs up and where it goes. There is no
command that edits it, and no command-line way to back up something it does
not mention: a path s3bak has never been told about is a path no command will
touch. This chapter documents every setting it can hold.

## Where the file lives

s3bak reads the file named by the environment variable **`S3BAK_CONFIG`**, and
falls back to **`~/.config/s3bak/config.py`** when that variable is unset or
empty. `S3BAK_CONFIG` names a file, not a directory, so it can be called
anything; it is how you keep more than one configuration and choose between
them per run.

The `~` in the default path is expanded from `HOME`, and only from `HOME`.
`XDG_CONFIG_HOME` is not consulted, so moving your configuration directory
elsewhere means setting `S3BAK_CONFIG`.

Nothing creates the file for you. Until it exists, every command stops with
the same message and a skeleton to start from:

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

Every command reads the file, including the ones that never contact S3. A
mistake in it is therefore found at once, by whatever you happen to run next,
rather than in the middle of a backup.

## The file is Python

The file is executed at startup, as an ordinary Python module, and s3bak then
reads its settings out of the names left behind. Some things follow from that.

The first is that a configuration can compute things. Paths can be built from
environment variables, entries can be added in a loop or behind an `if`, and
the same file can serve several machines:

```python
import os
import socket

HOME = os.environ["HOME"]

entries = {
    "bin": {"path": f"{HOME}/bin"},
}

if socket.gethostname() == "workstation":
    entries["work"] = {"path": f"{HOME}/work"}
```

The second is that names s3bak does not recognize are simply ignored, which is
what makes the helper variables above harmless. It also means a misspelled
setting is not an error. `max_concurency = 20` and an entry key of `exclude`
rather than `excludes` are both accepted in silence and both do nothing, so a
setting that appears to have no effect is worth spelling out loud before it is
worth investigating.

If executing the file raises, s3bak reports the exception with its type, which
is usually enough to recognize:

```console
$ s3bak list
s3bak: error loading /home/you/.config/s3bak/config.py: KeyError: 'NOPE'
```

Finally, being Python means the file runs with your privileges every time you
run s3bak. It deserves the same care as any other script you execute.

## All settings

At the top level of the file:

| Setting | Type | Default | What it sets |
| --- | --- | --- | --- |
| `profile` | non-empty string | *required* | the AWS profile to authenticate with; [Appendix A](appendix-a-aws-setup.md) creates one |
| `prefix` | non-empty string | *required* | the bucket and optional path everything is stored under; must start with `s3://` and name a bucket, and a trailing slash is accepted and ignored |
| `entries` | non-empty dict | *required* | what to back up, keyed by entry name; a name must be a non-empty, control-character-free single path component that does not end in `-manifest.jsonl` |
| `max_concurrency` | integer >= 1 | 10 | transfers running at once within one entry; also sizes the pool `verify --checksum` hashes with |
| `entry_concurrency` | integer >= 1 | 4 | entries processed at once by one command |
| `mtime_window` | number of seconds >= 0 | 0.01 | how far two modification times may differ and still count as equal; `0` demands an exact nanosecond match |

Each entry is a dictionary of these keys:

| Key | Type | Default | What it sets |
| --- | --- | --- | --- |
| `path` | non-empty, NUL-free string | *required* | the absolute local path to back up: a directory or a regular file, never a path root (`/`, or `C:\` or `\\server\share` on Windows), a symlink, or a special file; `~` is not expanded |
| `excludes` | list of NUL-free strings | nothing excluded | glob patterns to leave out of the backup, matched against paths relative to the entry root |
| `pre_hook` | non-empty list of NUL-free strings | no hook | argument vector run before every push attempt, without a shell; a failure stops the push |
| `post_hook` | non-empty list of NUL-free strings | no hook | argument vector run after a push that did work, without a shell; never runs after a push that changed nothing |
| `mtime_window` | number of seconds >= 0 | the top-level value | the same tolerance, for this entry alone |

That is the whole set; any other name in the file is ignored. A file missing
one of the required settings stops s3bak before it does anything at all. The
sections below cover the entries, and the settings worth more than a line.

## Entries

`entries` maps a name to a dictionary of settings:

```python
entries = {
    "bin": {"path": f"{HOME}/bin", "excludes": ["__pycache__/*"]},
    "wsl.conf": {"path": "/etc/wsl.conf"},
}
```

The name is both what you type on the command line and the path component the
backup lives under, so it has to be usable as both. Under a prefix of
`s3://my-bucket/backup`, the entry named `bin` above keeps its files at
`s3://my-bucket/backup/bin/...` and its manifest at
`s3://my-bucket/backup/bin-manifest.jsonl`.

A name must therefore work as a path component: `bin`, not `home/bin`. The
`-manifest.jsonl` ending is reserved for the same reason — it is how s3bak
names the manifest that sits beside the entry.

Within an entry, `path` is required and the rest are optional. What `path`
points at is checked when a command runs rather than when the file is read,
since a `pre_hook` is allowed to create it.

Refusing a path root is a guard rather than a limitation. A `path` is usually
computed rather than typed, and one that collapses to `/` would make a push
walk the whole machine and a `pull --delete` treat the whole machine as a tree
to mirror. That is never what was meant, so s3bak stops instead of asking.

A path root means a path with no parent, and nothing more. The test never
looks at the filesystem underneath, so a mount point is an ordinary directory
here: `/mnt/data` is a perfectly good entry.

### `excludes`

A list of strings: glob patterns for paths to leave out of the backup, matched
against paths relative to the entry root.

```python
"excludes": ["*.elc", "elpa/*"]
```

A pattern ending in `/*` names a directory, and drops it with everything under
it before the walk descends, so an excluded subtree costs nothing to skip. Any
other pattern is matched against individual paths. The patterns behave less
like a shell's than they look — each is matched against a whole relative path
rather than one name at a time, and `*` matches across directory separators —
so [How s3bak detects changes](04-change-detection.md) covers the language and
its consequences properly.

### `pre_hook` and `post_hook`

Commands to run around a push, each a non-empty list of strings:

```python
"pre_hook": ["/home/you/bin/dump-database"],
"post_hook": ["rclone", "copy", "/mnt/data/vault", "remote:vault"],
```

The list is an argument vector, not a command line. The first item is the
executable and each remaining item is passed as one argument, with **no shell
involved**: globbing, pipelines, redirection and `&&` are not interpreted, and
a pattern like `*.sql` reaches the program as those five characters. Anything
that needs a shell belongs in a script that the hook then names.

`pre_hook` runs before every push attempt, which is what makes it the place to
produce what is about to be backed up — a database dump, an export, a
generated archive. If it exits non-zero, the push does not happen.

`post_hook` runs after a push **that did work**. A push that found nothing to
change runs no post_hook at all, deliberately, so that a hook with side
effects does not fire on a backup that changed nothing.
[Operating s3bak](07-operating.md) covers using hooks in a routine, including
the journal a post_hook can read to learn what the push actually did.

## Tuning

The optional top-level settings default to values that suit most machines, and
the reason to change one is usually that a particular machine or filesystem
has forced the question. Beyond what the table says:

- **`max_concurrency`** is not taken from the AWS CLI's `[s3]` settings, so
  `s3.max_concurrent_requests` in `~/.aws/config` has no effect on s3bak and
  the value belongs here.
- **`entry_concurrency`** multiplies with the setting above: 4 entries each
  transferring 10 objects is 40 requests in flight, which is the number to
  think about on a metered or fragile link.
- **`mtime_window`** of `0` demands an exact nanosecond match.

The window exists because filesystems disagree about how precisely they store
a modification time, and a pull onto a filesystem coarser than the window
would otherwise see every restored file as changed and download it again on
the next run. The default absorbs the rounding of the common modern
filesystems; FAT32, HFS+ and WSL2's `/mnt/c` need more.
[How s3bak detects changes](04-change-detection.md) explains the comparison,
and [Platform notes](09-platform-notes.md) gives the values per filesystem.

The window can be set in more than one place, and the most specific one wins:

1. `--mtime-window` on the command line, which overrides both of the others;
2. the entry's own `mtime_window`;
3. the top-level `mtime_window`, or its default if that is unset as well.

## When the file is wrong

s3bak validates the whole file before doing anything, and a problem stops the
command with exit status 1 and a message naming the setting, the value and the
file:

```console
$ s3bak status bin
s3bak: entry_concurrency must be a positive integer in /home/you/.config/s3bak/config.py (got 0)
```

What it checks is every restriction in the tables above, for every entry
rather than only the one a command was pointed at. A broken entry is therefore
reported the next time you run anything at all, rather than the next time that
particular entry is backed up.

The NUL restriction is the one that looks gratuitous. Nobody types a NUL, but
a path or a hook argument assembled from a file or a subprocess can carry one,
and it would survive every other check before failing deep inside the first
filesystem call — with a traceback rather than a message.

## A complete example

```python
import os

HOME = os.environ["HOME"]

profile = "s3bak"
prefix = "s3://my-bucket/backup"

entry_concurrency = 2
mtime_window = 0.01

entries = {
    ".ssh": {"path": f"{HOME}/.ssh", "excludes": ["agent/*"]},
    "bin": {"path": f"{HOME}/bin", "excludes": ["__pycache__/*"]},
    ".emacs.d": {
        "path": f"{HOME}/.emacs.d",
        "excludes": ["*.elc", "elpa/*", "eln-cache/*"],
    },
    "wsl.conf": {"path": "/etc/wsl.conf"},
    "vault": {
        "path": "/mnt/data/vault",
        "mtime_window": 1,
        "post_hook": ["rclone", "copy", "/mnt/data/vault", "remote:vault"],
    },
}
```

Read `HOME` with `os.environ["HOME"]` rather than `os.environ.get("HOME")`, so
that an unset variable stops s3bak instead of turning `f"{HOME}/bin"` into
`/bin`. On Windows, build from `USERPROFILE`.

`config.example.py`, in the repository, is a longer version of the same file
with the reasoning inline.

## Next

- [How s3bak detects changes](04-change-detection.md) for what `excludes` and
  `mtime_window` do once a command is running.
- [Command reference](05-command-reference.md) for the options that override
  what is configured here.
- [Operating s3bak](07-operating.md) for hooks, unattended runs, and the rest
  of a working routine.
