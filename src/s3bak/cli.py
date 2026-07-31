# Requires Python 3.10+
"""s3bak - Unified S3 backup/restore tool.

Backs up and restores configured directories or files to/from S3.

Config: ~/.config/s3bak/config.py (override: $S3BAK_CONFIG)

This module is the entry point: it parses argv, resolves entry/path arguments,
runs selected entries (optionally in parallel), and dispatches to the
``cmd_*`` functions in ``commands``. The console-script ``s3bak`` calls ``run``.
The implementation is split across sibling modules:

    console   terminal I/O, warnings, path helpers
    store     the boto3-s3 backend (Boto3S3Store)
    localwalk the manifest walk (boto3-s3's engine, backup-style)
    config    config.py loading, Config / Opts
    manifest  the v3 JSONL manifest format + merge_join + ManifestFilter
    compare   manifest-vs-local diff and status/diff presentation
    restore   pull-side filesystem operations (apply metadata, prune extras)
    syncops   the manifest <-> S3 bridge and download orchestration
    commands  one cmd_* per subcommand
"""

from __future__ import annotations

import concurrent.futures
import math
import os
import posixpath
import queue
import shlex
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from importlib.metadata import version
from typing import NoReturn

from s3bak import manifest
from s3bak.commands import (
    cmd_diff,
    cmd_list,
    cmd_ls_remote,
    cmd_pull,
    cmd_push,
    cmd_show,
    cmd_status,
    cmd_verify,
    verify_top_level,
)
from s3bak.compare import _resolve_use_color
from s3bak.config import Config, Opts, load_config
from s3bak.confirm import AnswerMode, is_aborted, reset_confirmations, resolve_answer_mode
from s3bak.console import console, expand_home, normalize_local_path
from s3bak.restore import canonical_restore_comparison_path, resolve_pull_destination
from s3bak.store import Boto3S3Store
from s3bak.syncops import download_from_s3

# Names re-exported for the test suite (which drives s3bak through s3bak.cli):
# their canonical homes are the sibling modules imported above.
__all__ = [
    "Boto3S3Store",
    "Config",
    "Opts",
    "_resolve_use_color",
    "download_from_s3",
    "load_config",
    "main",
    "run",
    "run_entries",
]


# =============================================================================
# Parallel runner
# =============================================================================

# Default cap on how many entries a multi-entry command runs at once, when
# entry_concurrency is not configured (a configured value replaces this
# default outright, it does not further narrow it). Each entry worker's own
# sync/cp already drives s3transfer's own transfer pool (~10 threads by
# default), so 4 entries at once already means roughly 40 transfers in
# flight - enough to saturate typical bandwidth well before entry count would
# need to grow further. The cap also bounds how many clients (store.clone,
# below) and threads run_entries builds up front, which otherwise scales
# with entry count alone (one of each per entry, sight unseen).
_DEFAULT_ENTRY_CONCURRENCY = 4


def run_entries(
    fn: Callable[[Config, str, Opts], int],
    cfg: Config,
    entries: list[str],
    opts: Opts,
    *,
    serial: bool = False,
) -> int:
    if not entries:
        return 0
    if len(entries) == 1:
        return fn(cfg, entries[0], opts)

    if serial:
        # Interactive --delete: the per-item confirmation reads stdin on the
        # calling thread. Running entries on worker threads would leave a Ctrl-C
        # during a prompt unable to interrupt the blocking read (SIGINT reaches
        # only the main thread), hanging executor.shutdown(wait=True). Run the
        # entries sequentially on this thread so the prompt - and its interrupt -
        # stays here; a q abort then also stops the entries not yet run.
        statuses = []
        for entry in entries:
            if is_aborted():
                break
            statuses.append(fn(cfg, entry, opts))
        return next((status for status in statuses if status), 0)

    # One thread per entry, capped at _DEFAULT_ENTRY_CONCURRENCY unless
    # entry_concurrency overrides that default (see the constant above).
    cap = cfg.entry_concurrency if cfg.entry_concurrency is not None else _DEFAULT_ENTRY_CONCURRENCY
    workers = min(len(entries), cap)

    # boto3-s3's concurrency contract: transfers running on different threads
    # must not share a client, and clients must be built sequentially up
    # front. One store (own orchestrator + client) per worker slot, built
    # here on the main thread; each task borrows one for its duration, so a
    # worker's sync/cp never shares a client with another running transfer.
    stores: queue.SimpleQueue[Boto3S3Store | None] = queue.SimpleQueue()
    stores.put(cfg.store)
    for _ in range(workers - 1):
        stores.put(cfg.store.clone() if cfg.store is not None else None)

    def run_one(entry: str) -> int:
        store = stores.get()
        try:
            return fn(replace(cfg, store=store), entry, opts)
        finally:
            stores.put(store)

    statuses = [0] * len(entries)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {executor.submit(run_one, entry): index for index, entry in enumerate(entries)}
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            try:
                statuses[index] = future.result()
            except BrokenPipeError:
                # Output is gone (e.g. piped to a closed reader): let run()'s
                # handler map it to the documented 141 instead of a worker 1.
                raise
            except Exception as exc:
                console.err(f"{entries[index]}: {exc}")
                statuses[index] = 1
    finally:
        # SIGINT lands in this (main) thread as SystemExit: cancel the entries
        # that have not started, but let the running ones finish - killing an
        # entry mid-push would leave its manifest and data inconsistent. The
        # normal path has nothing pending, so this is then a plain shutdown.
        executor.shutdown(wait=True, cancel_futures=True)
    # Completion order varies with scheduling. Preserve the first configured
    # entry's failure so --all has a deterministic exit code (including a
    # post_hook's documented 3+ code) rather than whichever worker happened to
    # finish first.
    return next((status for status in statuses if status), 0)


def _validate_distinct_entries(resolved: Sequence[tuple[str, str | None]], command: str) -> None:
    seen: set[str] = set()
    for entry, _sub in resolved:
        if entry in seen:
            console.die(
                f"duplicate entry in {command}: {entry} "
                f"(parallel {command} of the same entry is not supported)"
            )
        seen.add(entry)


def _run_resolved_entries(
    fn: Callable[[Config, str, Opts, str | None], int],
    cfg: Config,
    resolved: Sequence[tuple[str, str | None]],
    opts: Opts,
    *,
    serial: bool = False,
) -> int:
    entries = [entry for entry, _sub in resolved]
    sub_by_entry = {entry: sub for entry, sub in resolved}

    def _run_one(cfg_: Config, entry_: str, opts_: Opts) -> int:
        return fn(cfg_, entry_, opts_, sub_by_entry.get(entry_))

    return run_entries(_run_one, cfg, entries, opts, serial=serial)


def _interactive_delete(opts: Opts) -> bool:
    """A --delete run whose confirmations are answered at an interactive prompt
    (not --yes, not a non-interactive all-no). Such a run must execute its
    entries serially on the main thread - see run_entries(serial=...)."""
    return opts.delete and resolve_answer_mode(yes=opts.yes) is AnswerMode.ASK


# =============================================================================
# Usage
# =============================================================================


@dataclass(frozen=True)
class _OptionSpec:
    display: str
    description: str
    error_label: str


@dataclass(frozen=True)
class _CommandSpec:
    overview: str
    summary: str
    usage: tuple[str, ...]
    arguments: tuple[tuple[str, str], ...]
    options: tuple[str, ...]
    sections: tuple[tuple[str, tuple[str, ...]], ...]
    examples: tuple[str, ...]


_OPTION_SPECS = {
    "all": _OptionSpec("--all", "Apply to all configured entries", "--all"),
    "dry_run": _OptionSpec("--dry-run", "Show changes without applying them", "--dry-run"),
    "delete": _OptionSpec(
        "--delete", "Delete destination items absent from the source", "--delete"
    ),
    "yes": _OptionSpec("--yes", "Confirm every deletion", "--yes"),
    "meta_only": _OptionSpec("--meta-only", "Sync only metadata; skip file data", "--meta-only"),
    "data_only": _OptionSpec(
        "--data-only", "Sync only file data; leave metadata unchanged", "--data-only"
    ),
    "checksum": _OptionSpec(
        "--checksum", "Compare file contents instead of size and mtime", "--checksum"
    ),
    "mtime_window": _OptionSpec(
        "--mtime-window <seconds>", "Override the mtime tolerance", "--mtime-window"
    ),
    "output": _OptionSpec(
        "-o, --output <path>", "Restore one target to this exact path", "-o/--output"
    ),
    "verbose": _OptionSpec("-v, --verbose", "Show detailed operations", "-v/--verbose"),
    "color": _OptionSpec("--color[=WHEN]", "Colorize output (auto, always, or never)", "--color"),
    "no_color": _OptionSpec("--no-color", "Disable color", "--no-color"),
    "help": _OptionSpec("--help", "Show this help", "--help"),
}

_DELETE_CONFIRMATION = (
    (
        "Deletion confirmation",
        (
            "--delete prompts with y/n/a/d/q: delete, keep, delete all, keep all, or quit.",
            "? explains the answers at the prompt; full words (yes/no/all/quit) also work.",
            "Without a TTY, every answer is no unless --yes is set.",
        ),
    ),
)

_COMMAND_SPECS = {
    "push": _CommandSpec(
        overview="Back up entries or sub-paths to S3",
        summary="Back up configured entries or selected sub-paths to S3.",
        usage=(
            "s3bak push [options] <entry|path>...",
            "s3bak push [options] --all",
        ),
        arguments=(("<entry|path>...", "Entries or paths to back up"),),
        options=(
            "all",
            "dry_run",
            "delete",
            "yes",
            "meta_only",
            "data_only",
            "checksum",
            "mtime_window",
            "verbose",
            "help",
        ),
        sections=_DELETE_CONFIRMATION,
        examples=(
            "s3bak push bin",
            "s3bak push bin/subdir",
            "s3bak push --all --dry-run",
            "s3bak push bin --delete",
        ),
    ),
    "pull": _CommandSpec(
        overview="Restore entries or sub-paths from S3",
        summary="Restore configured entries or selected sub-paths from S3.",
        usage=(
            "s3bak pull [options] <entry|path>...",
            "s3bak pull [options] --all",
        ),
        arguments=(("<entry|path>...", "Entries or paths to restore"),),
        options=(
            "all",
            "dry_run",
            "delete",
            "yes",
            "meta_only",
            "data_only",
            "checksum",
            "mtime_window",
            "output",
            "verbose",
            "help",
        ),
        sections=_DELETE_CONFIRMATION,
        examples=(
            "s3bak pull bin",
            "s3bak pull bin home-docs",
            "s3bak pull bin -o /tmp/restore",
            "s3bak pull bin --delete --dry-run",
        ),
    ),
    "show": _CommandSpec(
        overview="Print a backed-up file",
        summary="Print a single backed-up file to stdout.",
        usage=("s3bak show [options] <entry|path>",),
        arguments=(("<entry|path>", "Backed-up file to print"),),
        options=("verbose", "help"),
        sections=(),
        examples=(
            "s3bak show wsl.conf",
            "s3bak show bin/s3bak",
            "s3bak show /home/me/bin/s3bak",
        ),
    ),
    "status": _CommandSpec(
        overview="Compare local files with the backup",
        summary="Compare local files with the backup using metadata.",
        usage=(
            "s3bak status [options] <entry|path>...",
            "s3bak status [options] --all",
        ),
        arguments=(("<entry|path>...", "Entries or paths to compare"),),
        options=("all", "mtime_window", "verbose", "color", "no_color", "help"),
        sections=(
            (
                "Status letters",
                (
                    "These are push-oriented: they show what would change on the backup.",
                    "M <path>  Metadata differs between local and backup",
                    "A <path>  Only local; push would add it",
                    "D <path>  Only in backup; push --delete would remove it",
                ),
            ),
        ),
        examples=(
            "s3bak status bin",
            "s3bak status --all",
            "s3bak status -v bin",
            "s3bak status bin/s3bak",
        ),
    ),
    "verify": _CommandSpec(
        overview="Verify backup integrity on S3",
        summary="Verify that the manifest and the stored objects agree, changing nothing.",
        usage=(
            "s3bak verify [options] <entry|path>...",
            "s3bak verify [options] --all",
        ),
        arguments=(("<entry|path>...", "Entries or paths to verify"),),
        options=("all", "checksum", "mtime_window", "verbose", "help"),
        sections=(
            (
                "Checks",
                (
                    "Every manifest file record must have its data object, with the",
                    "recorded size and a restorable storage class; directory, symlink,",
                    "and special records must have none. Unrecorded objects and folder",
                    "objects are reported. --all also inventories the prefix top level.",
                    "--checksum additionally compares local file content against S3",
                    "ETags and flags edits the size+mtime check can never see.",
                    "Errors exit 1; a warnings-only run exits 2. Nothing is modified.",
                ),
            ),
        ),
        examples=(
            "s3bak verify --all",
            "s3bak verify bin",
            "s3bak verify --all --checksum",
        ),
    ),
    "diff": _CommandSpec(
        overview="Show content differences",
        summary="Show content differences between the backup and local files.",
        usage=("s3bak diff [options] <entry|path>",),
        arguments=(("<entry|path>", "Entry or path to compare"),),
        options=("verbose", "color", "no_color", "help"),
        sections=(),
        examples=(
            "s3bak diff bin",
            "s3bak diff bin/s3bak",
            "s3bak diff ~/bin/s3bak",
        ),
    ),
    "list": _CommandSpec(
        overview="List locally configured entries",
        summary="List locally configured entries. This command does not access S3.",
        usage=("s3bak list",),
        arguments=(),
        options=("help",),
        sections=(),
        examples=("s3bak list",),
    ),
    "ls-remote": _CommandSpec(
        overview="List entries or files stored on S3",
        summary="List configured entries or backed-up paths stored on S3.",
        usage=("s3bak ls-remote [options] [entry|path]",),
        arguments=(("[entry|path]", "Entry or sub-path to list"),),
        options=("verbose", "help"),
        sections=(),
        examples=(
            "s3bak ls-remote",
            "s3bak ls-remote bin",
            "s3bak ls-remote bin/subdir",
        ),
    ),
}


def _format_help_rows(rows: Sequence[tuple[str, str]]) -> list[str]:
    return [f"  {label:<28}{description}" for label, description in rows]


def _command_help_text(command: str) -> str:
    spec = _COMMAND_SPECS[command]
    lines = ["Usage:", *(f"  {usage}" for usage in spec.usage), "", spec.summary]
    if spec.arguments:
        lines.extend(("", "Arguments:"))
        lines.extend(_format_help_rows(spec.arguments))
    lines.extend(("", "Options:"))
    lines.extend(
        _format_help_rows(
            tuple(
                (_OPTION_SPECS[option].display, _OPTION_SPECS[option].description)
                for option in spec.options
            )
        )
    )
    for heading, section_lines in spec.sections:
        lines.extend(("", f"{heading}:", *(f"  {line}" for line in section_lines)))
    example_heading = "Example:" if len(spec.examples) == 1 else "Examples:"
    lines.extend(("", example_heading, *(f"  {example}" for example in spec.examples)))
    return "\n".join(lines) + "\n"


def print_usage(status: int = 1) -> NoReturn:
    config_path = os.environ.get("S3BAK_CONFIG") or expand_home("~/.config/s3bak/config.py")
    command_lines = "\n".join(
        f"  {command:<12}{spec.overview}" for command, spec in _COMMAND_SPECS.items()
    )
    text = f"""\
Usage: s3bak <command> [options] [args]

Back up and restore configured files and directories using S3.

Commands:
{command_lines}

Global options:
  --help      Show this help
  --version   Show the program version

Run 's3bak <command> --help' for command-specific help.

Config file: {config_path}
"""
    (sys.stdout if status == 0 else sys.stderr).write(text)
    sys.exit(status)


def print_command_help(command: str) -> NoReturn:
    sys.stdout.write(_command_help_text(command))
    sys.exit(0)


# =============================================================================
# Argument resolution
# =============================================================================


def _resolve_one_arg(cfg: Config, arg: str) -> tuple[str, str | None]:
    # A bare name is an entry. ``entry/sub`` is entry-rooted syntax independent
    # of CWD; every other path is resolved locally and matched to the containing
    # configured entry (longest root wins).
    seps = [os.sep, os.altsep] if os.altsep else [os.sep]
    if not (any(s in arg for s in seps) or os.path.isabs(arg)):
        if arg in cfg.entries:
            return arg, None
        console.die(f"no such entry: {arg}")

    if not os.path.isabs(arg):
        entry_form = arg
        for sep in seps:
            if sep and sep != "/":
                entry_form = entry_form.replace(sep, "/")
        name, separator, raw_sub = entry_form.partition("/")
        if separator and name in cfg.entries:
            sub = posixpath.normpath(raw_sub)
            if sub == ".":
                return name, None
            # os.path.splitdrive is identity on POSIX (so a filename containing
            # ':' is fine there) but strips a Windows drive: on Windows a
            # drive-qualified sub like "C:/escape" makes os.path.join(entry_path,
            # sub) discard the entry path entirely and escape the entry root.
            if (
                sub == ".."
                or sub.startswith("../")
                or sub.startswith("/")
                or os.path.splitdrive(sub)[0]
            ):
                console.die(f"sub path must stay inside entry {name}: {arg}")
            return name, sub

    local = normalize_local_path(arg)
    matches: list[tuple[int, str, str | None]] = []
    for name, entry_cfg in cfg.entries.items():
        raw_path: str = entry_cfg["path"]
        entry_path = normalize_local_path(raw_path)
        try:
            if os.path.commonpath((local, entry_path)) != entry_path:
                continue
        except ValueError:  # different Windows drives
            continue
        rel = os.path.relpath(local, entry_path)
        candidate_file = None if rel == os.curdir else rel.replace(os.sep, "/")
        matches.append((len(entry_path), name, candidate_file))

    if not matches:
        console.die(f"no such entry for path: {arg}")
    longest = max(length for length, _name, _sub in matches)
    best = [(name, sub) for length, name, sub in matches if length == longest]
    if len(best) > 1:
        names = ", ".join(sorted(name for name, _sub in best))
        console.die(f"path is ambiguous between entries {names}: {arg}")
    return best[0]


def resolve_entry_file(cfg: Config, positional: list[str], cmd: str) -> tuple[str, str | None]:
    if len(positional) != 1:
        console.die(f"{cmd} takes <entry> or <path>")
    return _resolve_one_arg(cfg, positional[0])


def resolve_entry_files(
    cfg: Config, positional: list[str], cmd: str
) -> list[tuple[str, str | None]]:
    if not positional:
        console.die(f"{cmd} requires at least one entry or path")
    return [_resolve_one_arg(cfg, arg) for arg in positional]


def _validate_pull_destinations(cfg: Config, resolved: Sequence[tuple[str, str | None]]) -> None:
    destinations: list[tuple[str, str, str]] = []
    for entry, sub in resolved:
        base_path: str = cfg.entries[entry]["path"]
        target = resolve_pull_destination(entry, base_path, sub, None)
        assert target is not None
        destinations.append(
            (entry, os.path.abspath(target), canonical_restore_comparison_path(target))
        )

    for index, (left_entry, left_path, left_cmp) in enumerate(destinations):
        for right_entry, right_path, right_cmp in destinations[index + 1 :]:
            try:
                common = os.path.commonpath((left_cmp, right_cmp))
            except ValueError:  # different Windows drives
                continue
            if common in (left_cmp, right_cmp):
                console.die(
                    "pull restore destinations overlap: "
                    f"{left_entry} ({left_path}) and {right_entry} ({right_path})"
                )


def _join_command_names(commands: list[str]) -> str:
    if len(commands) == 1:
        return commands[0]
    if len(commands) == 2:
        return f"{commands[0]} and {commands[1]}"
    return f"{', '.join(commands[:-1])}, and {commands[-1]}"


def _validate_command_options(command: str, used_options: Sequence[str]) -> None:
    allowed = _COMMAND_SPECS[command].options
    for option in used_options:
        if option in allowed:
            continue
        applicable_commands = [
            name for name, spec in _COMMAND_SPECS.items() if option in spec.options
        ]
        label = _OPTION_SPECS[option].error_label
        console.die(f"{label} only applies to {_join_command_names(applicable_commands)}")


# =============================================================================
# Main
# =============================================================================


def main(argv: list[str] | None = None) -> int:
    # A q answer to a --delete confirmation aborts via module state; clear it
    # here so in-process callers (the test suite) start every run fresh.
    reset_confirmations()

    args = sys.argv[1:] if argv is None else argv
    if not args:
        print_usage()

    subcmd = args[0]
    if subcmd == "--help":
        print_usage(0)
    if subcmd == "--version":
        sys.stdout.write(f"s3bak {version('s3bak')}\n")
        return 0
    if subcmd not in _COMMAND_SPECS:
        console.err(f"unknown command: {subcmd}")
        print_usage()

    opt_all = False
    opt_dryrun = False
    opt_delete = False
    opt_yes = False
    opt_meta_only = False
    opt_data_only = False
    opt_verbose = False
    opt_checksum = False
    opt_mtime_window: float | None = None
    opt_outpath: str | None = None
    opt_color: str = "auto"
    help_requested = False
    positional: list[str] = []
    used_options: list[str] = []

    def take_value(flag: str, idx: int) -> tuple[str, int]:
        # Support both --flag=value and --flag value
        if "=" in flag:
            return flag.split("=", 1)[1], idx
        if idx + 1 >= len(args):
            console.die(f"{flag} requires a value")
        return args[idx + 1], idx + 1

    i = 1
    while i < len(args):
        a = args[i]
        if a == "--all":
            opt_all = True
            used_options.append("all")
        elif a == "--dry-run":
            opt_dryrun = True
            used_options.append("dry_run")
        elif a == "--delete":
            opt_delete = True
            used_options.append("delete")
        elif a == "--yes":
            opt_yes = True
            used_options.append("yes")
        elif a == "--meta-only":
            opt_meta_only = True
            used_options.append("meta_only")
        elif a == "--data-only":
            opt_data_only = True
            used_options.append("data_only")
        elif a in ("-v", "--verbose"):
            opt_verbose = True
            used_options.append("verbose")
        elif a == "--checksum":
            opt_checksum = True
            used_options.append("checksum")
        elif a == "--mtime-window" or a.startswith("--mtime-window="):
            used_options.append("mtime_window")
            val, i = take_value(a, i)
            try:
                opt_mtime_window = float(val)
            except ValueError:
                console.die(
                    f"--mtime-window requires a non-negative number of seconds (got {val!r})"
                )
            if not math.isfinite(opt_mtime_window) or opt_mtime_window < 0:
                console.die(f"--mtime-window must be >= 0 (got {opt_mtime_window})")
            # Converted to an integer nanosecond count (window_ns_for); reject a
            # value so large the * 1e9 overflows to inf and would crash round().
            if not math.isfinite(opt_mtime_window * 1_000_000_000):
                console.die(f"--mtime-window is too large to use (got {opt_mtime_window})")
        elif a in ("-o", "--output") or a.startswith("--output="):
            used_options.append("output")
            opt_outpath, i = take_value(a, i)
            if "=" not in a and opt_outpath.startswith("-"):
                console.die(
                    f"{a} requires a path value (use --output=<path> for a path starting with '-')"
                )
        elif a == "--color":
            opt_color = "always"
            used_options.append("color")
        elif a.startswith("--color="):
            used_options.append("color")
            val = a.split("=", 1)[1]
            if val not in ("auto", "always", "never"):
                console.die(f"invalid --color value: {val} (use auto|always|never)")
            opt_color = val
        elif a == "--no-color":
            opt_color = "never"
            used_options.append("no_color")
        elif a == "--help":
            help_requested = True
            used_options.append("help")
        elif a == "--":
            positional.extend(args[i + 1 :])
            break
        elif a.startswith("-"):
            console.die(f"unknown option: {a}")
        else:
            positional.append(a)
        i += 1

    _validate_command_options(subcmd, used_options)

    opts = Opts(
        dryrun=opt_dryrun,
        delete=opt_delete,
        yes=opt_yes,
        meta_only=opt_meta_only,
        data_only=opt_data_only,
        verbose=opt_verbose,
        checksum=opt_checksum,
        outpath=opt_outpath,
        color=opt_color,
    )

    # Global option/command compatibility. Rejecting an inapplicable flag here
    # (rather than silently ignoring it) matters most for --dry-run: a command
    # that ignored it would perform the REAL operation the user believed was
    # a preview.
    if opt_all and positional:
        console.die("--all cannot be combined with explicit entries")
    if opt_meta_only and opt_data_only:
        console.die("--meta-only and --data-only are mutually exclusive")

    if opt_yes and not opt_delete:
        console.die("--yes requires --delete (it answers deletion confirmations)")
    if subcmd == "push" and opt_delete and opt_meta_only:
        console.die(
            "push --delete cannot be combined with --meta-only (a deletion drops the object too)"
        )
    if subcmd == "push" and opt_delete and opt_data_only:
        console.die(
            "push --delete cannot be combined with --data-only (a deletion drops the record too)"
        )

    if opt_checksum and opt_meta_only:
        console.die("--checksum cannot be combined with --meta-only (no file data is compared)")
    # push/pull --checksum replaces the size+mtime check entirely, so a window is
    # meaningless there. verify --checksum is the opposite: the window feeds the
    # stat classification of content mismatches, and is useless without it.
    if subcmd == "verify":
        if opt_mtime_window is not None and not opt_checksum:
            console.die(
                "--mtime-window requires --checksum with verify (it classifies content mismatches)"
            )
    elif opt_checksum and opt_mtime_window is not None:
        console.die(
            "--mtime-window cannot be combined with --checksum (content comparison ignores it)"
        )
    if subcmd == "pull" and opt_delete and opt_meta_only:
        console.die("pull --delete cannot be combined with --meta-only")

    if opt_outpath == "":
        console.die("-o/--output requires a non-empty path")
    if subcmd == "pull" and opt_outpath is not None:
        if opt_all:
            console.die("--all cannot be combined with -o/--output")
        if len(positional) > 1:
            console.die("-o/--output cannot be combined with multiple pull targets")

    if help_requested:
        print_command_help(subcmd)

    # Parsing and option validation deliberately precede config/S3 setup, so a
    # typo reports the typo even when the user's AWS profile is unavailable.
    # `list` needs config entries only and therefore does not construct a client.
    cfg = load_config(create_store=subcmd != "list")

    # A CLI --mtime-window overrides both the top-level and per-entry config
    # windows for this run (0 = exact). Affects the size+mtime check shared
    # by push / pull / status (see Config.window_for).
    if opt_mtime_window is not None:
        cfg.mtime_window_override = opt_mtime_window

    if subcmd == "push":
        if opt_all:
            resolved = [(entry, None) for entry in sorted(cfg.entries.keys())]
        else:
            resolved = resolve_entry_files(cfg, positional, "push")
        _validate_distinct_entries(resolved, "push")
        return _run_resolved_entries(
            cmd_push, cfg, resolved, opts, serial=_interactive_delete(opts)
        )

    elif subcmd == "pull":
        if opt_all:
            resolved = [(entry, None) for entry in sorted(cfg.entries.keys())]
        else:
            resolved = resolve_entry_files(cfg, positional, "pull")
        _validate_distinct_entries(resolved, "pull")
        _validate_pull_destinations(cfg, resolved)
        return _run_resolved_entries(
            cmd_pull, cfg, resolved, opts, serial=_interactive_delete(opts)
        )

    elif subcmd == "status":
        if opt_all:
            entries = sorted(cfg.entries.keys())
            status_sub_by_entry: dict[str, str | None] = {e: None for e in entries}
        else:
            resolved = resolve_entry_files(cfg, positional, "status")
            entries = [e for e, _ in resolved]
            status_sub_by_entry = {}
            for e, s in resolved:
                if e in status_sub_by_entry and status_sub_by_entry[e] != s:
                    console.die(f"conflicting sub paths for entry {e}")
                status_sub_by_entry[e] = s
            entries = list(status_sub_by_entry)

        def _status_one(cfg_: Config, entry_: str, opts_: Opts) -> int:
            return cmd_status(cfg_, entry_, opts_, sub=status_sub_by_entry.get(entry_))

        return run_entries(_status_one, cfg, entries, opts)

    elif subcmd == "verify":
        if opt_all:
            entries = sorted(cfg.entries.keys())
            verify_sub_by_entry: dict[str, str | None] = {e: None for e in entries}
        else:
            resolved = resolve_entry_files(cfg, positional, "verify")
            verify_sub_by_entry = {}
            for e, s in resolved:
                if e in verify_sub_by_entry and verify_sub_by_entry[e] != s:
                    console.die(f"conflicting sub paths for entry {e}")
                verify_sub_by_entry[e] = s
            entries = list(verify_sub_by_entry)

        def _verify_one(cfg_: Config, entry_: str, opts_: Opts) -> int:
            return cmd_verify(cfg_, entry_, opts_, sub=verify_sub_by_entry.get(entry_))

        rc = run_entries(_verify_one, cfg, entries, opts)
        if opt_all:
            # The top-level inventory closes the sweep: warnings only, so the
            # per-entry exit status stands and run() maps them to exit 2.
            verify_top_level(cfg, opts)
        return rc

    elif subcmd == "diff":
        entry, file = resolve_entry_file(cfg, positional, "diff")
        return cmd_diff(cfg, entry, opts, file)

    elif subcmd == "show":
        entry, file = resolve_entry_file(cfg, positional, "show")
        return cmd_show(cfg, entry, opts, file)

    elif subcmd == "list":
        if positional:
            console.die("list takes no arguments (use 'ls-remote <entry>' for manifest contents)")
        return cmd_list(cfg, opts)

    elif subcmd == "ls-remote":
        if len(positional) > 1:
            console.die("ls-remote takes at most one entry or path")
        if not positional:
            return cmd_ls_remote(cfg, opts, None, None)
        entry, sub = _resolve_one_arg(cfg, positional[0])
        return cmd_ls_remote(cfg, opts, entry, sub)

    raise AssertionError(f"unhandled command: {subcmd}")


def _sdk_errors() -> tuple[type[BaseException], ...]:
    """Return boto3-s3 / botocore operational error types lazily.

    An empty tuple when the SDK is unimportable ensures exception matching
    never masks the original failure with a second ImportError.
    """
    try:
        from boto3_s3 import Boto3S3Error
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        return ()
    return (Boto3S3Error, BotoCoreError, ClientError)


def run() -> int:
    """Console entry point: install signal handling and translate exceptions
    into exit codes. This is what the ``s3bak`` command invokes."""
    console.reset_warnings()
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
    try:
        rc = main() or 0
    except subprocess.CalledProcessError as e:
        cmd_str = shlex.join(e.cmd) if isinstance(e.cmd, list) else str(e.cmd)
        console.err(f"command failed: {cmd_str}")
        return e.returncode or 1
    except BrokenPipeError:
        return 141
    except FileNotFoundError as e:
        # An external command is not on PATH (the `diff` binary), or a required
        # file vanished mid-run: report cleanly instead of a raw traceback.
        console.err(str(e))
        return 1
    except OSError as e:
        # Permission errors, disk-full failures, and local I/O races are normal
        # operational failures for a backup tool, not reasons to expose a Python
        # traceback to the CLI user.
        console.err(str(e))
        return 1
    except manifest.ManifestError as e:
        console.err(str(e))
        return 1
    except _sdk_errors() as e:
        console.err(str(e))
        return 1
    # A run that only warned (skipped files etc.) but hit no hard error exits 2
    # (aws-style). Successful work is retained, but callers must inspect it.
    if rc == 0 and console.warning_count() > 0:
        return 2
    return rc
