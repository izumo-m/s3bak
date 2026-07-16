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
    manifest | console

lower-level foundations
```

Dependencies flow down the diagram and may skip rows. `manifest` and `console`
are foundational modules used from several layers.

## Module responsibilities

- **console** owns terminal output, warning accounting, and small path helpers.
  It imports no other s3bak module.
- **manifest** owns the JSONL format, validation, streaming subtree patches,
  sorted-stream joins, and the stat-based manifest comparison. It uses only the
  standard library.
- **localwalk** enumerates local trees in the same key order as the data sync
  and owns exclude pruning; the data sync's local side walks with the same
  walker (`sync_walker`), so excludes prune only the local side of a sync.
- **store** is the S3 boundary. `Boto3S3Store` wraps transfers, listing, and
  object inspection.
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
- **syncops** connects manifests and local trees to the S3 store. It writes and
  downloads manifests, orchestrates downloads, and builds sync comparison
  strategies.
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
