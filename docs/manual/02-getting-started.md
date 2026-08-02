# Getting started

This chapter goes from nothing to a working backup and back again: install
s3bak, write a configuration file, push one directory to S3, change it, push
again, and restore a file after losing it. Every command here runs against a
real bucket, and every output shown is what the command actually prints.

## Before you start

You need:

- **Python 3.10 or later.**
- **An S3 bucket, and an AWS profile that can read and write it.** If you do
  not have them yet, [Appendix A](appendix-a-aws-setup.md) creates them from
  scratch; come back here once `~/.aws/config` holds a profile for your
  bucket.
- **A `diff` executable.** Only the `s3bak diff` command uses it, so nothing
  in this chapter needs it, but installing it now saves a surprise later.

s3bak does not need the AWS CLI: it talks to S3 from inside its own process
and installs everything it needs for that. Have it installed anyway. When
something goes wrong with credentials, s3bak can only repeat what S3 told it,
whereas the AWS CLI can ask on its own behalf which credentials are in effect
and whether they reach the bucket. Appendix A uses it for both.

## Install s3bak

s3bak is installed with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install git+https://github.com/izumo-m/s3bak
```

`s3bak --version` prints the installed version, which confirms the command is
on your `PATH`. To try s3bak without installing it, run
`uvx --from git+https://github.com/izumo-m/s3bak s3bak --help` instead.

Later, `uv tool upgrade s3bak --reinstall` updates it. `--reinstall` is
required: the install tracks the repository's default branch rather than a
pinned commit, so without it uv may consider its cached clone current and skip
fetching new commits.

## Write the configuration file

s3bak reads `~/.config/s3bak/config.py`, or the file named by the environment
variable `S3BAK_CONFIG`. There is no command that creates it; write it
yourself. It is plain Python, executed at startup. A minimal one:

```python
import os

HOME = os.environ["HOME"]

profile = "s3bak"
prefix = "s3://my-bucket/backup"

entries = {
    "bin": {"path": f"{HOME}/bin"},
}
```

`profile` is the AWS profile s3bak authenticates with, `prefix` is the bucket
and optional path everything is stored under, and `entries` maps a name to the
local path it backs up. The name is what you type on the command line and
where the backup lives under the prefix, so it must be a single path
component: `bin`, not `home/bin`.

Some rules about `path` are worth knowing before your first run, because they
are easy to trip over:

- It must be **absolute**. A relative path would silently depend on the
  working directory the command happened to run in.
- **`~` is not expanded.** s3bak rejects `"~/bin"` rather than guessing what
  it meant, which is why the example builds the path from `HOME`. Reading
  `HOME` with `os.environ["HOME"]` rather than `os.environ.get("HOME")` is
  deliberate too: an unset variable then fails loudly, instead of turning
  `f"{HOME}/bin"` into `/bin` and aiming the backup at a system directory. On
  Windows, build from `USERPROFILE`.

Start with one small directory, as above. Your entire home directory as a
first entry takes a long time to upload and a long time to check, and this
chapter asks you to look at every line of output.
[Configuration](03-configuration.md) documents every key, including excludes
and hooks.

## Check what s3bak sees

`list` prints the configured entries. It reads the configuration and nothing
else, never contacting S3, which makes it the cheapest way to confirm that
s3bak found your file and understood it:

```console
$ s3bak list
bin                  /home/you/bin
```

If the file is not where s3bak looked, it says so and prints a skeleton to
start from. If the file is there but malformed, the message names the key at
fault.

## The first push

Preview before uploading anything. `--dry-run` runs the whole comparison for
real, using the same requests the real run uses, and stops short of every
change:

```console
$ s3bak push --dry-run bin
(dry-run) upload: /home/you/bin/backup-photos to s3://my-bucket/backup/bin/backup-photos
(dry-run) upload: /home/you/bin/lib/common.sh to s3://my-bucket/backup/bin/lib/common.sh
(dry-run) upload: /home/you/bin/sync-notes to s3://my-bucket/backup/bin/sync-notes
(dry-run) would update manifest: bin-manifest.jsonl
```

Every file is listed because nothing is backed up yet. This is the moment to
check that the paths on the left are the ones you meant to back up. Then run
it for real:

```console
$ s3bak push bin
upload: /home/you/bin/backup-photos to s3://my-bucket/backup/bin/backup-photos
upload: /home/you/bin/lib/common.sh to s3://my-bucket/backup/bin/lib/common.sh
upload: /home/you/bin/sync-notes to s3://my-bucket/backup/bin/sync-notes
Updating s3://my-bucket/backup/bin-manifest.jsonl
```

Uploads run in parallel, so the lines arrive in the order the transfers
finish, not in the order shown here. The last line writes the manifest: the
record of what this push left in the backup, which every later comparison
reads.

## Confirm the backup matches

```console
$ s3bak status bin
$
```

Silence is the answer. `status` prints one line per difference and nothing
else, so an empty result means the backup matches the local tree.

## Push again after a change

Edit `~/bin/backup-photos`, then add a new file `~/bin/lib/extra.sh`, and ask
again:

```console
$ s3bak status bin
M /home/you/bin/backup-photos	size, mtime
M /home/you/bin/lib	mtime
A /home/you/bin/lib/extra.sh
```

The letters describe what a push would change in the backup: `M` the stored
copy differs, `A` this exists only locally, `D` this exists only in the
backup. The tags after `M` name the properties that differ. `lib` is listed
because adding a file to a directory changes that directory's modification
time, and s3bak records and restores directory times like any other.

```console
$ s3bak push bin
upload: /home/you/bin/backup-photos to s3://my-bucket/backup/bin/backup-photos
upload: /home/you/bin/lib/extra.sh to s3://my-bucket/backup/bin/lib/extra.sh
Updating s3://my-bucket/backup/bin-manifest.jsonl
```

Two files moved. The two that had not changed were not uploaded, and not even
read: s3bak compared their size and modification time against the manifest,
which was enough to tell they still matched. That is what makes a routine push
cheap enough to run often. [How s3bak detects changes](04-change-detection.md)
explains the comparison, and when it needs help.

## Restore a file you lost

Move a file out of the tree to stand in for losing it:

```console
$ mv ~/bin/sync-notes /tmp/
$ s3bak status bin
M /home/you/bin	mtime
D /home/you/bin/sync-notes
```

`D` reads as a warning but is not one. The letters are push-oriented, so `D`
only means the backup has something the local tree does not; an ordinary push
keeps it. Deleting is opt-in in both directions, and
[Deleting safely](06-deleting-safely.md) covers it.

Pull the entry back:

```console
$ s3bak pull bin
download: s3://my-bucket/backup/bin/sync-notes to /home/you/bin/sync-notes
755 /home/you/bin/sync-notes
755 /home/you/bin
```

Only the missing file came down. The three that already matched were left
untouched, exactly as a push skips what has not changed. The lines without a
`download:` prefix are the metadata pass: each names a path whose recorded
permission bits and modification time s3bak has just applied. `sync-notes`
gets its executable bit back, and `bin` gets back the modification time that
the `mv` changed.

```console
$ s3bak status bin
$
```

A plain `pull` restores and repairs, but never removes: a file that exists
only locally is left where it is. Restoring an entry therefore cannot lose
local work by surprise.

## Next

You now have a working backup and have seen the whole cycle. From here:

- [Configuration](03-configuration.md) for every configuration key, the
  environment variables, and the pitfalls.
- [How s3bak detects changes](04-change-detection.md) for what "differs"
  means, and the one case where size and modification time are not enough.
- [Command reference](05-command-reference.md) for every command and option,
  including restoring to a different directory and printing a single file.
- [Deleting safely](06-deleting-safely.md) before you first use `--delete`.
- [Operating s3bak](07-operating.md) for a routine worth keeping: unattended
  runs, hooks, and how to satisfy yourself that the backup is still good.
