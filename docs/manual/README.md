# s3bak user manual

The complete guide to using s3bak. This manual is the authoritative
description of s3bak's observable behavior (see
[Documentation](../overview.md#documentation) in the design overview).

1. [Introduction](01-introduction.md) — what s3bak is: an rsync-like mirror
   of configured entries to an S3 prefix; how backups are stored; what s3bak
   deliberately does not do.
2. [Getting started](02-getting-started.md) — requirements, installation, a
   minimal `config.py`, and the first push, status, and pull.
3. [Configuration](03-configuration.md) — the config file: every key,
   environment variables, and pitfalls.
4. [How s3bak detects changes](04-change-detection.md) — the size+mtime
   comparison against the manifest, `mtime_window`, the blind spot and
   `--checksum`, and what excludes take out of the picture.
5. [Command reference](05-command-reference.md) — every command and option,
   entry and path resolution, output format, exit codes.
6. [Deleting safely](06-deleting-safely.md) — `push --delete` and
   `pull --delete`: confirmation prompts, `--yes`, and what can be lost.
7. [Operating s3bak](07-operating.md) — the recommended routine, unattended
   runs, hooks and `S3BAK_JOURNAL`, storage classes and bucket versioning, and
   keeping the backup private.
8. [Recovery and troubleshooting](08-recovery-troubleshooting.md) —
   interrupted runs, hard-kill residue, what to do about each verify finding,
   repairing a damaged manifest, and restoring onto a machine that has
   nothing.
9. [Platform notes](09-platform-notes.md) — Windows, macOS, WSL2: mtime
   granularity per filesystem, permissions and symlinks where they differ,
   name folding, moving a tree between platforms, and S3-compatible services.

Appendix:

- [A. Setting up AWS](appendix-a-aws-setup.md) — creating the bucket, the IAM
  policy and user, and the named profile s3bak authenticates with. One-time
  setup, needed before chapter 2 and never again.
