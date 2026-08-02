# Introduction

s3bak backs up files and directories to S3 and restores them again. It stores
your files as plain S3 objects, the way a straight upload would, and keeps a
manifest beside them recording everything an S3 object cannot represent. The
result behaves like an rsync mirror: a restore reproduces the tree rather than
approximating it, and each run moves only what actually differs.

## Why s3bak

An S3 object holds bytes and a name. It has no permission bits, no
modification time you control, no notion of a symbolic link, and no way to
exist as an empty directory. Upload a tree with ordinary S3 tooling and all of
that is simply gone, which is why the restore is the part that disappoints: a
private key comes back readable by everyone, a script comes back without its
executable bit, a symlink comes back as a second copy of its target, and an
empty directory some tool expects to find does not come back at all.

s3bak keeps the objects exactly as plain as they were: your files, unchanged,
one object each, readable with any S3 tool. What an object cannot hold goes
into a separate manifest stored alongside them, and everything else follows
from that pairing. A pull can restore the tree faithfully because the manifest
says what each path was. A comparison can be cheap because the manifest holds
the state of the last push, so s3bak can tell what changed without reading
file contents or listing the bucket. And what would change can be reported
before anything is touched, because the comparison is a step of its own rather
than a side effect of transferring.

What you need to use it:

- Python 3.10 or later.
- An S3 bucket you control, and an AWS profile with credentials for it. Other
  S3-compatible services are supported too; see
  [Platform notes](09-platform-notes.md).
- A `diff` executable, used by the `s3bak diff` command only.

## What a backup preserves

A push records, and a pull restores:

- **file contents**, byte for byte;
- **permission bits**, so a private key or an executable script comes back as
  it was;
- **modification times**, including those of directories and, where the
  platform can set them, symbolic links;
- **symbolic links**, as links with their recorded targets rather than as
  copies of what they point at;
- **empty directories**, which get no object of their own but are recorded all
  the same.

It does not restore:

- **hard links** — each linked path is backed up and restored as an
  independent file, so the link relationship is lost;
- **owner and group** — recorded and reported, but never applied: a
  root-owned file comes back owned by whoever ran the pull;
- **ACLs, extended attributes, and file sparseness** — not recorded at all;
- **special files** — a device node, FIFO, or socket is recorded, and its mode
  and modification time are applied when one already exists at that path, but
  a missing one is reported as an error rather than created.

A tree pushed on one operating system may also not restore identically on
another; [Platform notes](09-platform-notes.md) covers those differences.

## The model

You give s3bak a list of **entries**. An entry is a name paired with one
absolute local path, either a directory or a single file, and every command
works in terms of those names rather than paths. Everything s3bak stores lives
under one configured **prefix**, a bucket and an optional path within it.

`push` copies an entry's current local state up to that prefix, `pull` copies
it back down, and `status` reports what differs without touching either side.
Both directions are mirrors rather than plain transfers: s3bak compares the
two sides and moves only what differs. The default comparison is by size and
modification time, so an unchanged file is never re-read; see
[How s3bak detects changes](04-change-detection.md).

Deleting is not part of that by default. A push keeps the backup of a file you
deleted locally, and a pull keeps local files the backup does not know about.
`--delete` makes a direction a true mirror, and asks before removing anything;
see [Deleting safely](06-deleting-safely.md).

s3bak assumes a single operator running one command at a time. It locks
nothing: running two s3bak commands against the same configuration at once, or
modifying a tree while it is being pushed, are yours to avoid.

## How a backup is stored

Each entry gets a tree of data objects under the prefix, and one manifest
object beside it. For a prefix of `s3://my-bucket/backup` and an entry named
`bin`:

```text
s3://my-bucket/backup/bin/...                data objects, one per regular file
s3://my-bucket/backup/bin-manifest.jsonl     the manifest
```

A regular file whose path inside the entry is `sub/tool.py` is stored at
`.../bin/sub/tool.py`, byte for byte. s3bak does not wrap, archive, compress,
or otherwise transform file contents, and the local path above the entry root
is not part of the key. Any S3 tool can therefore browse the backup and
download a file from it, with or without s3bak. A single-file entry stores its
one object at the entry key itself, with its manifest named after that same
key.

Only regular files become objects. A directory, a symbolic link, or a special
file has no object of its own: it exists in the manifest, a JSON Lines file
that records the entry's tree, listing each path with its type, permission
bits, size, modification time, and symlink target. The manifest is the record
of the last push.

So reading a file back out needs nothing but S3, while reconstructing the tree
exactly — empty directories, symlinks, permissions, timestamps — needs the
manifest, which s3bak interprets for you.

## What s3bak does not do

**No history.** A backup holds one state: the one the last push left. There
are no generations, snapshots, or point-in-time restores, and pushing a
changed file overwrites the object that held its previous contents. To be able
to go back, enable **versioning on the bucket**, an S3 feature that s3bak
neither turns on nor manages (see [Operating s3bak](07-operating.md)).

**No snapshot consistency.** s3bak reads the filesystem as it is while the
push runs. A file being written at that moment can upload half-written, and a
set of files that must agree with one another can be captured mid-change. Dump
a live database with a `pre_hook` and back up the dump instead.

**No encryption of its own.** s3bak does not encrypt data before uploading it,
and does not ask for any particular server-side encryption mode. Objects are
encrypted at rest by the storage service according to the bucket's
configuration; S3 applies SSE-S3 by default and offers no unencrypted mode.
What s3bak does not add is client-side encryption: anyone who can read the
bucket can read your files, so the bucket policy and the credentials that
reach it are what keep a backed-up `~/.ssh` private (see
[Operating s3bak](07-operating.md)).

## Next

[Getting started](02-getting-started.md) walks through installing s3bak,
writing a minimal `config.py`, and running the first push, status, and pull.
If you do not have a bucket and a profile yet,
[Appendix A](appendix-a-aws-setup.md) prepares them first.
