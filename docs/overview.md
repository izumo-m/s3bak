# s3bak design overview

s3bak backs up and restores configured directories or single files to and from
S3. For each entry it stores the file **data** as plain S3 objects and a
**manifest** object alongside, so it can report and restore metadata (mode,
mtime, ownership, symlinks) that raw S3 objects do not carry, and decide what a
push or pull would change without re-reading file contents.

This document is the entry point to the design docs:

- **[manifest.md](manifest.md)** — the on-S3 manifest format (v3 JSONL) and the
  key-ordered tree walk that produces it.
- **[sync.md](sync.md)** — the compare model (what counts as changed), the
  transfer path (direct client call vs. multipart `S3.cp`), and the push / pull
  pipelines.

The design is discussed in chat until settled, then the settled parts land
here. These docs describe behaviour and invariants, not line-by-line code, so
they age with the design rather than with the source.

## What is on S3

Everything for the configured `prefix = s3://<bucket>/<path_prefix>` lives under
that prefix. For an entry named `bin`:

```
s3://bucket/backup/bin/...                 # the data objects (one per file)
s3://bucket/backup/bin-manifest.jsonl      # the metadata manifest
```

- **Data objects** mirror the local tree: `bin/sub/x.txt` → `…/bin/sub/x.txt`.
  Directories, symlinks, and empty directories have **no** data object; they
  exist only in the manifest. A single-file entry stores one object at the
  entry key (`…/bin`).
- **The manifest** (`<entry>-manifest.jsonl`) records every path in the tree
  with its mode / owner / group / size / mtime / symlink target. It is the
  source of truth for `status`, for restoring metadata on `pull`, and for the
  default sync comparison. The `-manifest.jsonl` suffix cannot collide with a
  single-file entry's own data key (unlike a bare `<entry>.json` would).

## Module architecture

The code is split into single-responsibility modules with a one-directional
dependency graph (no import cycles). Lower layers never import upper ones.

```
          cli            argv parsing, entry/path resolution, run(), exit codes
           │
        commands         one cmd_* per subcommand; orchestrates the layers below
        ┌──┴────────────┬───────────────┐
     compare          restore         syncops     status/diff · restore FS ops · manifest⇄S3
        │                │            ┌──┴───┐
        │                │         config   store   config load · the boto3-s3 backend
        └────────┬───────┴────────────┴───────┘
              manifest        console         the v3 format (pure) · terminal I/O (pure)
```

- **console** — terminal output, the warning counter, and small path helpers.
  Depends on nothing in s3bak; everything depends on it.
- **manifest** — the v3 JSONL format: parse / format, the S3-key-order tree
  walk, the streaming sub-tree patch, and the stat-based `ManifestFilter`
  compare. Pure (stdlib only), so it is unit-testable in isolation.
- **store** — `Boto3S3Store`, the thin S3 backend over the boto3-s3 library
  (transfers, listing, head-object). Builds one shared client up front.
- **config** — loads the user's `config.py`, validates it, and attaches a ready
  store. Defines `Config` and `Opts`, the two values threaded through commands.
- **compare** — `compare_to_local` (manifest record vs. local file) plus the
  status/diff presentation (color, humanized sizes/durations).
- **restore** — pull-side filesystem operations: manifest→target path
  resolution, applying recorded metadata, the Windows read-only prep, and
  pruning local extras.
- **syncops** — the manifest⇄S3 bridge: writing manifests, downloading a
  manifest or data tree, and building the sync `compare=` strategy.
- **commands** — one `cmd_*` per subcommand, composing the layers into
  push / pull / status / diff / show / list / ls-remote.
- **cli** — parses argv, resolves entry/path arguments, runs entries
  (optionally in parallel under `--all`), dispatches to `commands`, and maps
  exceptions to exit codes. The console script `s3bak` calls `cli.run`.

## Design principles

- **The manifest is the record of the last real push.** Only a push rewrites
  it. `status` and both compare directions read it; nothing else mutates it.
- **Decide by stat, not by content, in the common case.** The default compare
  is an rsync-style size+mtime quick check against the manifest — no file is
  read. Content comparison (ETag) is opt-in via `--checksum` (see
  [sync.md](sync.md)).
- **Prefer one round trip.** Small objects use a direct `GetObject` /
  `PutObject`; only files at or above the multipart threshold go through
  `S3.cp`'s s3transfer machinery. Downloads skip the pre-transfer HeadObject
  probe s3transfer would issue.
- **Thread-safe by construction.** `--all` runs entries concurrently, and each
  sync spawns its own transfer threads. boto3 client *construction* is not
  thread-safe, so the store builds one client up front (single-threaded) and
  hands every S3 location to the library already bound to it.
- **Break cleanly, migrate by re-push.** This is a personal tool: there is no
  backward-compatibility layer and no dead code kept "just in case". A format
  change is migrated by re-running `push --all`, which regenerates every
  manifest.

## Configuration

s3bak reads `~/.config/s3bak/config.py` (override with `$S3BAK_CONFIG`). It is
plain Python executed at startup. Required: `profile`, `prefix` (must start
with `s3://`), and `entries`. Each entry has a required `path` and optional
`excludes` / `pre_hook` / `post_hook` / `mtime_window` (per-entry override of
the window below). Optional top-level tuning knobs:

| Setting             | Default | Meaning                                                        |
| ------------------- | ------- | -------------------------------------------------------------- |
| `max_concurrency`   | 10      | transfer threads for `cp` / `sync`                             |
| `compare_workers`   | =above  | parallel ETag comparisons under `--checksum`                   |
| `entry_concurrency` | all     | how many entries run at once under `--all`                     |
| `mtime_window`      | 0.01 (s) | quick-check mtime tolerance (fractional ok); `0` = exact match |

See [`../config.example.py`](../config.example.py) for a commented template.

## Commands

| Command     | Purpose                                                              |
| ----------- | ------------------------------------------------------------------- |
| `push`      | back up entries or sub-paths to S3                                  |
| `pull`      | restore an entry or sub-path (`--all` for every entry)              |
| `status`    | compare local vs. backup, metadata only (M / A / D)                 |
| `diff`      | content diff between backup and local                               |
| `show`      | print one backed-up file to stdout                                  |
| `list`      | list locally configured entries (no S3 access)                     |
| `ls-remote` | list S3 entries, or files recorded under an entry / sub-path        |

Options: `--all`, `--dry-run` (push), `--delete` (pull), `--meta-only`,
`--data-only`, `--checksum` (push/pull), `--mtime-window <seconds>`,
`-o/--output` (pull), `-v/--verbose`, `--color[=WHEN]` / `--no-color`. Push/pull
semantics and the meaning of each mode are in [sync.md](sync.md).

An `<entry>/<sub>` or a local path argument (containing `/`) resolves to the
owning entry plus a sub-path; a bare name resolves to an entry. Path arguments
are matched against entry paths, longest prefix wins.

## Exit codes

`cli.run` translates outcomes into process exit codes:

| Code | Meaning                                                                 |
| ---- | ---------------------------------------------------------------------- |
| 0    | success                                                                |
| 1    | an error (bad usage, missing entry, SDK error, corrupt manifest, …)    |
| 2    | completed but **warned** — e.g. an unreadable/skipped file; the backup |
|      | is incomplete but the rest succeeded and the manifest was updated      |
| 3+   | a failing `post_hook` propagates its own exit code                     |
| 130  | interrupted (SIGINT)                                                    |
| 141  | broken pipe (e.g. `show … | head`)                                      |

Exit 2 is deliberately distinct from 0 so an incomplete backup is detectable in
scripts, and from 1 so it is not confused with a hard failure.

## Testing

The suite (`uv run pytest`) is hermetic: it drives `cli.main` in-process against
moto's in-memory S3 mock — no network, Docker, or credentials. `scripts/` brings
up a local MinIO stack for manual testing against a real endpoint.
