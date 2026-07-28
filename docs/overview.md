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

## Design documents

- **[Storage model](storage.md)** — how local trees, data objects, and manifests
  are represented under the configured S3 prefix.
- **[Manifest format](manifest.md)** — the JSONL format, validation rules,
  ordering, and streaming invariants.
- **[Sync model](sync.md)** — comparison and transfer strategies, concurrency,
  and the push and pull pipelines.
- **[Push journal](journal.md)** — the single-scan push: the journal of
  manifest changes the compare emits, its format, and the streaming manifest
  rewrite it drives.
- **[Verification model](verify.md)** — the read-only manifest ↔ S3 integrity
  check, its finding severities, and the suggested verification routine.
- **[CLI contract](cli.md)** — argument resolution, explicit option handling,
  concurrent result aggregation, and exit codes.
- **[Internal architecture](architecture.md)** — module responsibilities,
  dependency direction, and shared S3 client construction.
