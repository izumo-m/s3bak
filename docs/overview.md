# s3bak design overview

s3bak backs up and restores configured files and directories using S3 as its
storage.

## Project goals

### Reliable restoration

A backup is successful only if it can restore the intended filesystem state.
s3bak preserves file data together with the filesystem metadata needed for a
reliable restore.

### Transparent storage

Regular-file contents are stored unchanged as S3 objects that mirror the
configured local path hierarchy. Backups remain easy to inspect and retrieve
with standard S3 tooling, even where s3bak is unavailable.

### Safe and predictable operation

Intended changes should be observable, command behaviour should be explicit,
and operations should fail safely when stored state cannot be trusted.

### Supported environments

Support Windows, Linux, and macOS with Python 3.10 or later.

### S3 compatibility

s3bak aims to support S3-compatible services broadly. When a service lacks an
S3 capability required by s3bak, an appropriate compatibility approach is
evaluated separately.

### Performance and scalability

Everything that processes a tree streams. The manifest, the local walk, and
the S3 listing all ascend in S3 key byte order, so every multi-record
operation is a merge-join with a bounded lookahead — memory stays independent
of file count. The invariant is precise about its allowances:

- an **ancestor stack** bounded by directory depth, for the operations that
  are inherently post-order (settling a directory's metadata after its
  children; removing children before their directory);
- a **per-directory sort** bounded by one directory's direct entries (key
  order has to be produced from an unsorted readdir);
- **deferred work** bounded by the number of actual type conflicts, never by
  tree size.

Disk use follows the same rule: content is staged at most one object at a
time, and intermediate state that could grow with the tree spools to
temporary files. I/O, S3 access, and concurrency should continue to improve
within this invariant, without compromising correctness. See
[manifest.md](manifest.md) for the ordering contract the merge-joins rely on.

### Maintainable evolution

Responsibilities and dependency directions should remain explicit, and
behaviour should be verified with automated tests. Superseded implementations
are removed, and a clear current design is preferred over backward-
compatibility layers.

## Scope

s3bak is designed for personal use by a single, attentive operator. Problems
that the operator can avoid simply by taking care are out of scope for the
tool itself, for example:

- Running multiple s3bak invocations against the same configuration at the
  same time.
- Backing up a directory while it is being modified.

s3bak does not detect or guard against these conditions; avoiding them is
the operator's responsibility.

### Trust boundary

s3bak trusts its own bucket and its own local filesystem. Neither is treated
as attacker-controlled input, and s3bak does not try to defend against one
that is — because at that point there is nothing left to defend:

- **An attacker who can write the bucket already owns the backup.** They can
  rewrite any recorded object and the manifest itself, and a pull faithfully
  restores what the backup says. Elaborate paths such as escaping the restore
  root through a symlink gain them nothing they could not get by editing the
  target file's own object. This is solved one layer down, with AWS-side
  access control — a bucket policy, source-IP restriction, SSO-issued
  short-lived credentials — not inside s3bak.
- **An attacker who can write the local tree does not need s3bak at all.**
  Racing a check against its use, to make s3bak write a file on their behalf,
  is a detour around simply writing that file.

So a guard is only worth having here when it holds with **no attacker at
all**. Confinement to the restore destination is one of those: a pull that
writes outside the tree the operator named is a blast-radius bug in ordinary
single-operator use, reachable through nothing more hostile than a symlink,
an interrupted push's unrecorded object, or a filesystem that spells a name
differently than the manifest does. Those are treated as correctness bugs,
on the same footing as any other, and cross-platform restore fidelity is
their most common source (see [storage.md](storage.md)).

## Versioning

s3bak follows [Semantic Versioning](https://semver.org/). While the version
is below 1.0.0, MAJOR stays at 0: a release containing a spec change bumps
MINOR, and a fixes-only release bumps PATCH.

Notable changes are tracked in [CHANGELOG.md](../CHANGELOG.md), in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format. The commit
that makes a notable change also adds it to the `[Unreleased]` section.
Internal-only changes (refactor, performance, tests, documentation) add no
entry and do not by themselves warrant a release.

Whether a release bumps MINOR or PATCH is decided at release time from the
content of `[Unreleased]`: Added / Changed / Removed entries mark a spec
change (MINOR); Fixed-only content — restoring documented behavior — marks a
PATCH. A release is cut with a `chore(release): bump version to X.Y.Z`
commit on `develop` that turns `[Unreleased]` into the new version heading
and raises `version` in `pyproject.toml`, followed by a merge to `main` and
an annotated tag `vX.Y.Z`.

## Documentation

The user manual in [manual/](manual/README.md) is the authoritative
specification of s3bak's observable behavior; the design documents below and
the implementation are judged against it. What the manual does not describe
— internal rationale and invariants, such as the streaming invariant above
and the manifest ordering contract — is owned by the design documents. When
the manual and the implementation disagree, neither side wins by default:
which one is wrong is decided case by case, and that side is fixed.

### Design documents

Each records why its part of s3bak is built the way it is and what the
implementation must preserve, and leaves the observable behavior it produces
to the manual.

- **[Storage model](storage.md)** — how local trees, data objects, and manifests
  are represented under the configured S3 prefix.
- **[Manifest format](manifest.md)** — the JSONL format, validation rules,
  ordering, and streaming invariants.
- **[Sync model](sync.md)** — comparison and transfer strategies, concurrency,
  and the push and pull pipelines.
- **[Exclusion model](excludes.md)** — the aws-cli pattern semantics behind
  `excludes`, the ignore rule, and what exclusion does to the manifest.
- **[Push journal](journal.md)** — the single-scan push: the journal of
  manifest changes the compare emits, its format, and the streaming manifest
  rewrite it drives.
- **[Verification model](verify.md)** — the read-only manifest ↔ S3 integrity
  check: what each pass can prove, and the severity rule behind its findings.
- **[Interruption and recovery](recovery.md)** — the fail-old ordering that
  makes a re-run sufficient, what an unfinished command leaves behind, and the
  residues a hard kill cannot clean up.
- **[CLI contract](cli.md)** — argument resolution, explicit option handling,
  concurrent result aggregation, and the reasoning behind the exit codes.
- **[Internal architecture](architecture.md)** — module responsibilities,
  dependency direction, and shared S3 client construction.
