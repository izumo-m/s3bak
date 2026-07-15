# CLI contract

The CLI turns command-line input into an entry and an optional entry-relative
subpath, rejects ambiguous or inapplicable input before doing work, and maps
command outcomes to stable process exit codes. The complete command and option
reference is emitted by `s3bak --help`; this document records the design
contracts behind that interface.

## Entry and path resolution

A positional `<entry|path>` argument is resolved by shape:

- A bare name resolves only as a configured entry name.
- `<entry>/<subpath>` is entry-rooted syntax. It is independent of the current
  working directory, and the normalized subpath must stay inside the entry.
- Every other argument is normalized as a local path and matched against the
  configured entry roots that contain it. The longest containing root wins, so
  a nested entry is preferred over its parent.
- If equally specific entries match, or no entry contains the path, resolution
  fails instead of choosing implicitly.

The rules are shared by commands that accept entries or paths, so the same
argument denotes the same stored object or subtree across commands.

## Multiple pull targets

`pull <entry|path>...` resolves every argument before starting any restore and
runs distinct entries through the shared entry worker pool. Multiple arguments
that resolve to the same entry are rejected, even when they select different
subpaths, because parallel restores must not mutate the same entry tree.

The resolved restore destinations for a multi-target pull must be disjoint.
Equal destinations and ancestor/descendant pairs are rejected before S3 work;
the same check applies to `pull --all`. Existing symlinks in each destination's
parent chain are resolved for this comparison, while the final component is not
followed because pull may replace it. Case-only and Unicode-normalization-only
variants are conservatively treated as the same path on every platform; on a
case-sensitive filesystem, restore such entries separately. This prevents one
restore, especially a `--delete` restore, from writing or removing another
restore's files.

`-o/--output` names the exact destination for one target and is therefore
rejected with multiple explicit targets as well as with `--all`.

## Explicit input handling

Unknown options, invalid combinations, and options that do not apply to the
selected command are errors rather than ignored input. In particular, an
unsupported preview-like option must never allow a mutating command to proceed
as if it were a dry run.

Argument and option validation happens before configuration creates an S3
client. Local syntax errors therefore remain visible even when credentials or
the remote service are unavailable. The `list` command loads configuration
without constructing an S3 client because its result is entirely local.

## Concurrent entry results

Commands that operate on several entries may run them concurrently. Results
are retained in input order, and the process returns the first non-zero status
in that order rather than the first worker to finish. Under `--all`, entries
are sorted first, making the aggregate result deterministic.

Worker exceptions are reported for the affected entry and converted to status
1. Completed work from other entries is retained.

## Exit codes

`cli.run` translates command results and operational exceptions into process
exit codes:

| Code | Meaning |
| ---: | ------- |
| `0` | The command completed successfully without warnings. |
| `1` | A usage, configuration, local I/O, S3, or manifest error prevented a successful result. |
| `2` | Work completed but emitted at least one warning, such as a skipped unreadable entry. |
| `3+` | A failing hook or another command result propagated its non-zero status. |
| `130` | The process was interrupted by `SIGINT`. |
| `141` | Output ended because of a broken pipe. |

Status 2 distinguishes retained but incomplete work from both success and a
hard failure, allowing scripts to require inspection without discarding work
that did complete.

A `--delete` confirmation answered no — including the automatic no of a
non-interactive run without `--yes` — is a successful outcome (status 0), not
a warning: keeping a backup is a valid answer. Answering q aborts the command
with status 1. The exception is the explicit backup-subtree deletion
(`push --delete entry/gone-sub`): declining its one confirmation exits 1,
because the deletion was the command's entire purpose.
