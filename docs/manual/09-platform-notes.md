# Platform notes

s3bak runs on Linux, macOS, Windows and WSL2, and one backup can be pushed
from one of them and pulled onto another. What changes between them is not
s3bak but the filesystem underneath: how finely it stores a modification time,
which names it can hold, and how much of a permission bit it keeps. This
chapter is the per-platform detail the earlier chapters point at, plus what it
takes to use an S3-compatible service instead of AWS.

## Modification-time granularity

The comparison that decides what to transfer is size and modification time,
with `mtime_window` as the tolerance ([How s3bak detects
changes](04-change-detection.md)). The window exists because the manifest
records nanoseconds while a filesystem stores whatever precision it has: pull
a nanosecond timestamp onto a filesystem that keeps whole seconds and the file
comes back with a modification time that is *not* the recorded one, so without
a tolerance every later run would see a difference and download it again.

| Filesystem | Modification times it keeps | `mtime_window` |
| --- | --- | --- |
| ext4, XFS, btrfs, APFS | nanoseconds | the default `0.01`, or `0` for exact |
| NTFS | 100 ns | the default `0.01` |
| exFAT | 10 ms | the default `0.01` |
| HFS+ | 1 s | `1` |
| FAT32 | 2 s | `2` |
| WSL2 `/mnt/c` (drvfs) | reads 100 ns, but writes whole seconds | `1` or more |

The default of 10 ms covers the filesystems most machines actually run on.
The rest need the window raised — for the whole configuration, or for just the
entry that lives on such a filesystem
([Configuration](03-configuration.md)).

### Recognizing the symptom

A window that is too small for the filesystem shows up as the same file
transferring on every run, and as a `status` line that never goes away:

```console
$ s3bak status demo
M /home/you/demo/notes.txt	mtime
```

`-v` prints the two timestamps, and this is where the sub-second cases become
readable: when the difference is under a second, s3bak prints the fractional
digits rather than two identical-looking timestamps.

```console
$ s3bak status -v demo
+ (boto3) get_object s3://my-bucket/backup/demo-manifest.jsonl
M /home/you/demo/notes.txt	mtime
      mtime: remote=2026-08-14 13:56:25.944048102 < local=2026-08-14 13:56:26.244048102 (+0.3s)
```

A drift of a second or more prints whole seconds instead:

```console
$ s3bak status -v demo
+ (boto3) get_object s3://my-bucket/backup/demo-manifest.jsonl
M /home/you/demo/notes.txt	mtime
      mtime: remote=2026-08-14 13:56:25 < local=2026-08-14 13:56:28 (+3s)
```

The distinction matters when you are diagnosing. A drift smaller than the
filesystem's own resolution is rounding, and the answer is a larger window; a
drift of seconds is a real edit, and a larger window would only hide it.

`--mtime-window` tries a value without editing anything:

```console
$ s3bak status --mtime-window 1 demo
$
```

Silence means that window would settle it, and it belongs in `config.py` — on
the entry if only that tree needs it, at the top level if the whole machine
does.

For a filesystem not in the table, that is the whole procedure: pull, run
`status -v`, and read the drift the restored files report. The window wants to
be a little larger than the rounding you see, and no larger.

## Windows

### Permissions are one bit

Python on Windows does not report Unix permission bits. `os.stat` synthesizes
them: `0o666` for a writable file, `0o444` for a read-only one. s3bak
therefore compares **only the owner-write bit** there, and a restore applies
only what that bit can express.

What follows from it:

- A tree pushed from Linux keeps its recorded modes in the manifest — `0o600`
  stays `0o600` in the record, and `ls-remote` still shows it — but restoring
  it on Windows produces an ordinary writable file. **A private file does not
  come back private.** Windows ACLs are what protect it there, and s3bak
  records no ACLs at all.
- A read-only file on Windows restores as read-only, since that bit does
  survive the round trip.
- `status` on Windows reports a `mode` difference only when the writable bit
  differs, so a tree moved from Linux does not report every file as changed.

A pull that has to overwrite a read-only file clears the read-only bit for the
duration and puts the recorded mode back afterwards, so a read-only file is
not an obstacle to restoring over it.

### Symbolic links

Creating a symbolic link on Windows is a privileged operation unless
**Developer Mode** is on; otherwise the shell must be elevated. Without that,
a pull reports the failure for each symlink record and carries on with the
rest of the tree.

Windows also distinguishes a *file* symlink from a *directory* symlink, chosen
when the link is created. s3bak decides by looking at what the link's recorded
target resolves to; when that target does not exist yet — a link that sorts
before the directory it points at — the link is placed at the end of the
restore, once every directory exists and the probe can be trusted.

A symbolic link's own modification time is not restored on Windows, because
the platform cannot set it without following the link. That is not a gap in
the backup: the same limitation makes s3bak skip symlink modification times in
the comparison there, so nothing reports a difference it cannot fix.

### Junctions

A directory junction (`mklink /J`) is not a symbolic link as far as Windows is
concerned, and it is not one to `lstat` either — it looks like an ordinary
directory. So:

- **A push descends into a junction** and backs up the files it leads to as if
  they lived there. The backup ends up holding a second copy of that tree,
  under the junction's path.
- **A pull replaces a junction** that sits where the manifest records a real
  directory, and restores the recorded contents into a real directory.

If a junction is a shortcut rather than content you mean to back up, exclude
its path ([Configuration](03-configuration.md)).

### Names Windows cannot store

A name that is legal on Linux can be impossible on Windows, and the restore is
where that surfaces — the backup itself is fine.

- **Characters** — `< > : " | ? *` cannot appear in a Windows filename.
- **Trailing dots and spaces** are trimmed by Win32, so `report.` and
  `report` become the same file.
- **Reserved device names** — `CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`,
  `LPT1`–`LPT9` — cannot be used as filenames.
- **A backslash in a POSIX filename** is a path separator on Windows. A record
  like `./a\b` is a single file on Linux and a file `b` inside a directory `a`
  here, so s3bak refuses it before anything is written: `manifest path
  component contains a backslash, unrestorable on Windows`.
- **A component that looks like a drive** — a file literally named `C:` — is
  refused the same way, since joining it onto the restore root would discard
  the root: `manifest path component is drive-qualified, unrestorable on
  Windows`.

Long paths are the other limit: unless long-path support is enabled, a path
over 260 characters fails. Restoring into a short root (`C:\r` rather than
`C:\Users\you\Documents\restores\2026`) is usually enough to get a deep tree
back.

### Paths, arguments, and the rest

- **Build entry paths from `USERPROFILE`**, the way the examples build them
  from `HOME` ([Configuration](03-configuration.md)). `~` is not expanded in a
  configured `path`.
- **Both separators work** in a command-line argument: `s3bak push demo\lib`
  and `s3bak push demo/lib` mean the same thing.
- **A path root is refused** as an entry path — `C:\` and `\\server\share`
  alike — for the same reason `/` is.
- **Exclude patterns are written with `/`**, though `\` works too: a backslash
  in a pattern folds to `/` before matching. A drive-relative pattern like
  `C:cache` anchors to the entry root as `cache` rather than to a drive.
- **`s3bak diff` needs a `diff` executable** on `PATH`. Windows has none by
  default; the one shipped with Git for Windows works.

## macOS

macOS is a POSIX system, so permissions, symbolic links and their modification
times all behave as they do on Linux. Two things are specific to it:

- **The filesystem is case-insensitive by default.** APFS and HFS+ are both
  usually formatted that way, so `Report.txt` and `report.txt` are one file.
  Two recorded paths that differ only by case land on top of each other, and
  the last one restored wins.
- **Unicode normalization.** HFS+ stores names in NFD, so a name pushed from
  Linux in NFC comes back spelled differently byte-for-byte, though it looks
  identical. APFS preserves what it is given but still compares names both
  ways.

Both are name folding, and the section below covers what s3bak does about it.

On modification times, APFS keeps nanoseconds and needs no special window;
HFS+ keeps whole seconds and wants `mtime_window = 1`.

## WSL2

WSL2 is two filesystems with very different properties, and which one an entry
lives on decides how it behaves.

**The Linux filesystem** (`/home/...`, inside the VM) is ordinary ext4.
Everything works as it does on Linux, and the default window is right.

**A Windows drive under `/mnt`** (`/mnt/c/...`, the drvfs translation layer)
is where the differences are:

- **Modification times read at 100 ns but are written at whole seconds.** A
  pull restores a nanosecond timestamp and the filesystem keeps the second, so
  the next run sees a sub-second difference — forever, until the window
  absorbs it. An entry under `/mnt/c` wants `mtime_window` of at least `1`:

  ```python
  entries = {
      "work": {"path": "/mnt/c/Users/you/work", "mtime_window": 1},
  }
  ```

- **Permission bits depend on how the drive is mounted.** Without the
  `metadata` mount option, drvfs synthesizes modes from the Windows read-only
  attribute rather than storing real ones, so a `chmod` — including the one a
  pull applies — does not survive. With `metadata` on (in `/etc/wsl.conf`),
  Unix modes are stored and behave normally.
- **It is slower**, sometimes by a lot, since every operation crosses between
  the two worlds. A large tree on `/mnt/c` is worth pushing from Windows
  itself rather than through WSL2.

The reverse direction — backing up a WSL2 configuration file such as
`/etc/wsl.conf` — is an ordinary single-file entry, and the manual's examples
use exactly that.

## When the filesystem folds names

A **name-folding** filesystem is one where two different spellings reach the
same file: case-insensitive lookup on Windows and macOS, NFC/NFD equivalence
on macOS, and Win32's trimming of trailing dots and spaces.

The trouble it causes is specific. A push from Linux records `Report.txt`; a
pull onto a folding filesystem restores it into a file the local walk then
reports as `report.txt`. One file, two spellings — and to a comparison that
works on names, that looks like a record with nothing local plus a local file
with no record. Left alone, `pull --delete` would offer to delete the very
file it had just restored.

s3bak refuses to do that. When a local extra's name folds onto a path the
manifest records, it is kept, with a warning rather than a question:

```console
warning: not removed (a local name the filesystem may fold onto a recorded path): /home/you/demo/report.txt
```

The run exits 2, and the extra stays. It is a deliberately conservative test —
the fold is applied on every platform, not only where the filesystem really
folds — because the cost of being wrong in the other direction is a deleted
file.

The same conservatism guards a pull that would restore two entries into one
place. Destinations are compared folded, so a pair that differs only by case
is refused even on a filesystem where the two are genuinely different
directories:

```console
$ s3bak pull demo upper
s3bak: pull restore destinations overlap: demo (/home/you/demo) and upper (/home/you/DEMO)
```

What neither protection can do is make two recorded paths that differ only by
case restorable onto one folding filesystem. If a tree holds `Makefile` and
`makefile`, only one survives the restore, and s3bak has no way to tell you
which. Renaming one before it becomes a backup is the only real fix.

## Moving a tree between platforms

The backup is a plain set of objects plus a manifest, so nothing stops a push
from one platform and a pull onto another. What arrives is what the target
platform can represent:

| Pushed on | Pulled on | What changes |
| --- | --- | --- |
| Linux/macOS | Windows | permissions collapse to read-only or not; symlinks need Developer Mode; illegal names fail per record |
| Windows | Linux/macOS | modes come from the one bit Windows reported — expect `0o666`/`0o444` shapes rather than the original Unix bits |
| Linux | macOS | names may come back in a different Unicode normalization; case-only siblings collide |

The general limits — hard links, owner and group, ACLs and extended
attributes, special files — are the same everywhere and are listed in the
[Introduction](01-introduction.md).

Two habits make this painless. **Push from the machine that owns the data**,
so the manifest records the real metadata rather than a translated version of
it. And **rehearse the restore on the platform you would restore onto**: pull
into a scratch directory there and look at what arrives ([Operating
s3bak](07-operating.md)).

## S3-compatible services

s3bak has no endpoint setting of its own. It authenticates with the AWS
profile named in `config.py` and takes everything — credentials, region, and
the endpoint — from that profile, so pointing it at another service is a
matter of the AWS configuration alone:

```ini
# ~/.aws/config
[profile s3bak]
region = us-east-1
endpoint_url = https://s3.example.net
aws_access_key_id = ...
aws_secret_access_key = ...
```

```python
# ~/.config/s3bak/config.py
profile = "s3bak"
prefix = "s3://my-bucket/backup"
```

Some services want path-style URLs rather than a bucket in the hostname,
which is the same file:

```ini
s3 =
    addressing_style = path
```

s3bak's own development runs against a local MinIO this way, so the path is
well travelled. What the service has to provide is the ordinary object API:
`ListObjectsV2` with prefixes and delimiters, `GetObject`, `PutObject`,
`HeadObject`, `DeleteObject` and batched `DeleteObjects`, and multipart upload
for files above the multipart threshold.

Two things are worth checking before trusting a backup to one:

- **ETags.** `--checksum` compares a locally reconstructed ETag against the
  one the service reports, which assumes AWS's rule: an MD5 for a single-part
  upload, and the multipart form for the rest. A service that computes ETags
  differently makes `--checksum` report differences that are not there. Test
  it on a small entry — `s3bak verify --checksum <entry>` on a freshly pushed
  tree should be quiet.
- **Storage classes.** `verify` recognizes `GLACIER` and `DEEP_ARCHIVE` as
  archived. A service with its own tier names is invisible to that check, and
  a pull is what finds out ([Operating s3bak](07-operating.md)).

Everything else in this manual applies unchanged, including the bucket-side
advice — versioning, lifecycle rules, and least-privilege credentials — to
whatever extent the service implements it.

## Next

This is the last chapter. For what to do next:

- [Operating s3bak](07-operating.md) for the routine that keeps a backup
  trustworthy.
- [Recovery and troubleshooting](08-recovery-troubleshooting.md) when
  something reports a problem.
- [Command reference](05-command-reference.md) for the exact behavior of any
  command or option.
