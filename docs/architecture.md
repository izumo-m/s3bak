# Internal architecture

s3bak is divided into single-responsibility modules with a one-directional
dependency graph. Lower layers do not import orchestration layers, and the
graph contains no import cycles.

```text
higher-level orchestration

    cli
    commands
    restore | syncops
    compare | config | localwalk
    store
    confirm
    manifest | console | excludes

lower-level foundations
```

Dependencies flow down the diagram and may skip rows. `manifest` and `console`
are foundational modules used from several layers.

## Module responsibilities

- **console** owns terminal I/O, warning accounting, and small path helpers. A
  single `Console` holds both streams: writes are serialized by line, and a
  prompt session (`Console.prompt`) holds the terminal from the question to
  the answer, so a transfer's result line can never land between them. It
  imports no other s3bak module.
- **manifest** owns the JSONL format, validation, the push-journal format and
  its streaming merge, sorted-stream joins, and the stat-based manifest
  comparison pull uses.
- **excludes** owns the exclusion predicate (`Excludes`), delegated to
  boto3-s3's `globsieve` - aws-cli's `--exclude` engine - per
  [excludes.md](excludes.md). It imports no other s3bak module, so every
  layer that must agree on what an exclude means shares the one predicate.
- **localwalk** enumerates local trees in the same key order as the data sync
  and applies the entry's excludes ([excludes.md](excludes.md)); the data
  sync's local side walks with the same walker (`sync_walker`), so the sync
  and the manifest cannot disagree on what an exclude means, and excludes
  filter only the local side of a sync.
- **store** is the S3 boundary. `Boto3S3Store` wraps transfers, listing, and
  object inspection.
- **confirm** owns the deletion confirmations: answer modes, the per-item
  prompt, and the one-question subtree confirmation. Every question runs
  inside a console prompt session, which is what serializes them. It depends
  only on `console`.
- **config** loads and validates executable `config.py`, constructs the store
  when a command needs S3, and defines the `Config` and `Opts` values passed
  through the command layers.
- **compare** compares manifest records with local state and presents status
  and content diff output. Its predicate is shared by `status`, the pull no-op
  gate, and restore's gated metadata apply.
- **restore** owns pull-side filesystem mutation: target resolution, metadata
  application, conflict handling, and deletion of local extras. It also owns
  the keyed manifest / local-walk streams (`manifest_keyed` / `local_keyed`)
  whose merge-join drives both the metadata apply and, via commands, the
  status and `pull --delete` diffs.
- **syncops** connects manifests and local trees to the S3 store. It writes
  and downloads manifests, orchestrates downloads, owns push's journal
  emitter (`PushJournal`, the single-scan compare and keep/drop policy of
  [journal.md](journal.md)), and builds pull's comparison strategy.
- **commands** implements one `cmd_*` function per subcommand and composes the
  lower layers into complete operations.
- **cli** parses and validates arguments, resolves entries and paths, dispatches
  commands, coordinates entry-level concurrency, and maps outcomes to process
  exit codes.

## S3 client lifetime

An S3 command constructs one `Boto3S3Store` while loading configuration, before
entry worker threads start. The store creates one boto3-s3 `S3` object and one
boto3 client, then binds every S3 location to that client.

boto3-s3's concurrency contract is that transfers running on different threads
must not share a client, and that clients must be built sequentially (client
construction is not thread-safe). A multi-entry command therefore builds one
store per worker slot up front on the main thread (`Boto3S3Store.clone`), and
each entry task borrows one for its duration — a running sync/cp never shares
a client with another. Within one entry, s3transfer's own worker threads
operate under that entry's client, which the library manages. The local-only
`list` command does not construct a store. Runtime concurrency and its
configuration are described in [sync.md](sync.md#concurrency).
