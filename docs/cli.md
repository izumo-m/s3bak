# CLI contract

The CLI turns command-line input into an entry and an optional entry-relative
subpath, rejects ambiguous or inapplicable input before doing work, and maps
command outcomes to stable process exit codes. The reference for what each
command and option does is the manual's
[command reference](manual/05-command-reference.md) and `s3bak <command>
--help`; this document records the design contracts behind that interface.

## Help output

`s3bak --help` is an overview for choosing a command; `s3bak <command> --help`
is the reference for one command, including its status letters and examples.
The split keeps the top-level help readable as the number of commands grows.

Explicit help is written to standard output and exits 0; usage shown because
of missing input or an unknown command goes to standard error and exits 1.
Help never loads configuration or creates an S3 client, so it works on a
machine that has neither.

## Entry and path resolution

A positional `<entry|path>` argument is resolved by shape:

- A bare name resolves only as a configured entry name, or as a configured
  group name where the command accepts several entries.
- `<entry>/<subpath>` is entry-rooted syntax. It is independent of the current
  working directory, and the normalized subpath must stay inside the entry. A
  leading component that names a configured entry — or a configured group — is
  read this way rather than as a local path, so it shadows a relative path of
  the same name.
- Every other argument is normalized as a local path and matched against the
  configured entry roots that contain it. The longest containing root wins, so
  a nested entry is preferred over its parent.
- If equally specific entries match, or no entry contains the path, resolution
  fails instead of choosing implicitly.

The rules are shared by every command that accepts entries or paths, so the
same argument denotes the same stored object or subtree across commands — the
property that lets a user copy an argument from one command to another without
re-reading its rules.

A group is expanded in place, before anything else looks at the result: the
duplicate and conflict checks below, the pull destination check, and the
commands themselves all see entries only, which keeps a group from being a
second kind of target the rest of the CLI would have to understand. Expansion
is also why a group is rejected wherever an argument needs a root of its own:
in the entry-rooted syntax, whose subpath would have nothing to be relative
to, and in the single-target commands (`diff`, `show`, `ls-remote`), which
never expand a group at all — there a group name is not a way of naming a
single entry, however few entries it stands for. `pull -o` is the exception
that counts resolved targets instead, because its one argument is expanded
like any other. Group names are validated against the entry-name namespace
when the configuration loads, so a bare name never denotes both.

## Multiple pull targets

`pull <entry|path>...` resolves every argument before starting any restore and
runs distinct entries through the shared entry worker pool. Arguments that
resolve to exactly the same target are deduplicated rather than rejected —
after group expansion, asking for one thing twice is a repetition and not a
contradiction. Two arguments that name the same entry with *different*
targets — the entry itself beside a subpath of it, or two different
subpaths — remain an error, because parallel restores must not mutate the
same entry tree.

The resolved restore destinations for a multi-target pull must be disjoint.
Equal destinations and ancestor/descendant pairs are rejected before S3 work;
the same check applies to `pull --all`. Existing symlinks in each destination's
parent chain are resolved for this comparison, while the final component is not
followed because pull may replace it. Case-only and Unicode-normalization-only
variants are conservatively treated as the same path on every platform, since
the cost of being wrong is one restore deleting or overwriting another's
files — especially under `--delete`.

`-o/--output` names the exact destination for one target and is therefore
rejected with `--all`, and with more than one positional argument — that count
is taken before the configuration is read, since it needs nothing from it. The
single argument it does take is expanded like any other, so it may be a group,
provided that group stands for exactly one entry.

## Explicit input handling

Unknown options, invalid combinations, and options that do not apply to the
selected command are errors rather than ignored input. In particular, an
unsupported preview-like option must never allow a mutating command to proceed
as if it were a dry run.

Option and syntax validation happens before configuration creates an S3
client, so an option typo reports the typo even when credentials or the
remote service are unavailable. Entry and path resolution needs the loaded
configuration and therefore runs after the store exists. The `list` command
and `hook` load configuration without constructing an S3 client, because
neither touches S3 state.

## Hook invocation (`hook pre|post`)

`s3bak hook pre|post <entry>` runs one configured hook on demand, outside any
push, under the same contract as a push-run hook (argument vector, no shell,
stdin detached, the same exit-status normalization) with `S3BAK_JOURNAL`
unset — there is no push, hence no journal. The first positional argument
selects the hook; the rest are entry or group names, resolved by the same
rules every other multi-entry command uses and aggregated by the shared entry
worker pool.

The ways of naming entries mean different things, which is why they behave
differently when an entry has no such hook:

- **Naming an entry is an instruction**, so an entry without that hook fails
  (exit 1): silence would read as success, and a `post_hok:` typo in the
  configuration would be invisible.
- **`--all` means "every configured hook of this kind"**, so an entry without
  one is outside the operation's domain and is skipped (reported under `-v`).
  This keeps a real hook failure's exit status from being shadowed by a
  hook-less entry. The command errors only when no entry configures the hook
  at all — an instruction that turns out to be a no-op in full is not success.
- **Naming a group is an instruction on the group**, read like `--all`
  narrowed to its members: hook-less members are outside the domain and are
  skipped (reported under `-v`, once per entry however many groups named it).
  A member named explicitly as well keeps the strict reading, since the
  instruction to run that entry was given directly. The error is left for the
  group that is a no-op in full — no member configures the hook, and no member
  was named outright either — and it is a resolution failure, so it stops the
  command before any hook has run.

This is the one place where group expansion is not simply a substitution of
entry names, because the hook's domain rule has to apply to what the group
means rather than to each name it produced.

## Concurrent entry results

Commands that operate on several entries may run them concurrently. Results
are retained in input order, and the process returns the first non-zero status
in that order rather than the first worker to finish. Under `--all`, entries
are sorted first, making the aggregate result deterministic — a repeated run
gives a repeatable answer.

Worker exceptions are reported for the affected entry and converted to status
1. Completed work from other entries is retained.

## Exit codes

`cli.run` translates command results and operational exceptions into the
process exit codes the manual documents
([exit codes](manual/05-command-reference.md#exit-codes)). The design points
behind that mapping:

- **Status 2 exists to separate retained-but-incomplete work from both success
  and failure.** A run that skipped an unreadable file did its job and still
  needs a human, and a script must be able to tell that from a clean run
  without discarding the work that completed.
- **Hook statuses are normalized where they would collide** with s3bak's own
  meanings: a hook exiting 2 maps to 1 (2 is the warnings-only signal), and a
  hook killed by signal `N` maps to `128+N` rather than leaking a negative
  returncode into `sys.exit`.
- **`diff` overloads exit 1** with "content differs", the `diff(1)` / `git
  diff` convention, so exit 0 from `diff` means identical rather than merely
  "ran without error".
- **A `--delete` answered no is a success**, including the automatic no of a
  non-interactive run without `--yes`: keeping a backup is a valid answer.
  Aborting with `q` is a failure, and so is declining the explicit
  backup-subtree deletion — in both cases the command did not do what it was
  asked ([sync.md](sync.md#deleting-backups---delete---yes)).
- **Operational exceptions do not reach the user as tracebacks.** `run()`
  catches the SDK, OS, and manifest error families and reports what the layer
  below said, because a backup tool's failures are ordinary operational
  events rather than bugs.
