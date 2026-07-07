# Requires Python 3.10+
"""s3bak - Unified S3 backup/restore tool.

Backs up and restores configured directories or files to/from S3.

Config: ~/.config/s3bak/config.py (override: $S3BAK_CONFIG)

This module is the entry point: it parses argv, resolves entry/path arguments,
runs entries (optionally in parallel under --all), and dispatches to the
``cmd_*`` functions in ``commands``. The console-script ``s3bak`` calls ``run``.
The implementation is split across sibling modules:

    console   terminal I/O, warnings, path helpers
    store     the boto3-s3 backend (Boto3S3Store)
    config    config.py loading, Config / Opts
    manifest  the v3 JSONL manifest format + walk + ManifestFilter
    compare   manifest-vs-local diff and status/diff presentation
    restore   pull-side filesystem operations (apply metadata, prune extras)
    syncops   the manifest <-> S3 bridge and download orchestration
    commands  one cmd_* per subcommand
"""

from __future__ import annotations

import concurrent.futures
import os
import shlex
import signal
import subprocess
import sys
from collections.abc import Callable
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
)
from s3bak.compare import _resolve_use_color
from s3bak.config import Config, Opts, load_config
from s3bak.console import (
    die,
    err,
    expand_home,
    normalize_local_path,
    reset_warnings,
    warning_count,
)
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


def run_entries(
    fn: Callable[[Config, str, Opts], int],
    cfg: Config,
    entries: list[str],
    opts: Opts,
) -> int:
    if not entries:
        return 0
    if len(entries) == 1:
        return fn(cfg, entries[0], opts)

    # One thread per entry by default; cap at entry_concurrency when configured.
    workers = len(entries)
    if cfg.entry_concurrency is not None:
        workers = min(workers, cfg.entry_concurrency)

    agg = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fn, cfg, e, opts): e for e in entries}
        for future in concurrent.futures.as_completed(futures):
            try:
                st = future.result()
                if st and not agg:
                    agg = st
            except Exception as exc:
                entry = futures[future]
                err(f"{entry}: {exc}")
                if not agg:
                    agg = 1
    return agg


# =============================================================================
# Usage
# =============================================================================


def print_usage(status: int = 1) -> NoReturn:
    config_path = os.environ.get("S3BAK_CONFIG") or expand_home("~/.config/s3bak/config.py")
    text = f"""\
Usage: s3bak <command> [options] [args]

Commands:
  push <entry|path>...         Back up entries or sub-paths to S3
  pull <entry|path>            Restore an entry or sub-path (use --all for every entry)
  show <entry|path>            Print a single file from the backup to stdout
  status <entry|path>...       Compare local vs backup (metadata only)
  diff <entry|path>            Show content diff between backup and local
  list                         List locally configured entries
  ls-remote [entry|path]       List S3 entries, or files under an entry/sub-path
  help                         Show this help

Options:
  --all            Apply the command to all configured entries
  --dry-run        Show what would happen without changing anything (push)
  --delete         Delete extras: pull removes local files not in the backup;
                   a sub-path push removes S3 orphans under the sub-path
                   (a whole-entry push always mirrors)
  --meta-only      Sync only metadata (the manifest), skip file data (push/pull)
  --data-only      Sync only file data, leave manifest/local-meta untouched (push/pull)
  --checksum       Compare by content (ETag) instead of the manifest size+mtime
                   check; reads every candidate file (push/pull)
  --mtime-window <seconds>  Override config's size+mtime-check tolerance for
                   this run (fractional ok, 0 = exact); affects push/pull/status
  -o, --output <path>  Restore destination for pull (default: entry's configured path)
  -v, --verbose    Verbose output (details per field in status)
  --color[=WHEN]   Colorize status (verbose) and diff output
                   (WHEN: auto|always|never; default auto).
                   --color alone == --color=always. Honors NO_COLOR env var.
  --no-color       Disable color (same as --color=never)
  -h, --help       Show this help

status letters (push-oriented: what would change on the backup):
  M <path>         modified (metadata differs between local and backup)
  A <path>         only locally, not in backup   (push would add)
  D <path>         only in backup, not locally   (push would delete)

Config file: {config_path}

Examples:
  # push: back up one or more entries (or sub-paths)
  s3bak push bin .bash.d               # push selected entries
  s3bak push --all                     # push every configured entry
  s3bak push --all --dry-run           # preview without uploading
  s3bak push --meta-only bin           # upload metadata (the manifest) only
  s3bak push --meta-only --all         # upload metadata for all entries
  s3bak push --data-only bin           # upload data only, leave manifest unchanged
  s3bak push bin/s3bak                 # single file inside the bin entry
  s3bak push ~/bin/s3bak               # same, via ~ expansion
  s3bak push bin/subdir                # only the sub-directory

  # pull: restore from the backup (single entry/path; use --all for every entry)
  s3bak pull bin                       # restore to the configured path
  s3bak pull bin -o /tmp/restore       # restore to an alternative path
  s3bak pull bin --delete              # also remove local files not in backup
  s3bak pull --all                     # restore every entry in parallel
  s3bak pull --meta-only bin           # restore metadata only (no file download)
  s3bak pull --data-only bin           # restore file data only (no mode/mtime applied)
  s3bak pull bin/s3bak                 # restore a single file
  s3bak pull bin/subdir -o /tmp/restore # restore a sub-tree elsewhere

  # show: print a single backed-up file to stdout
  s3bak show wsl.conf                  # single-file entry (no slash = entry name)
  s3bak show bin/s3bak                 # local path, CWD-relative
  s3bak show ~/bin/s3bak               # local path with ~ expansion
  s3bak show /home/me/bin/s3bak | less # absolute local path

  # status: compare local vs backup (metadata only, both directions)
  s3bak status bin                     # M/A/D summary for one entry
  s3bak status --all                   # status of every entry
  s3bak status -v bin                  # verbose per-field differences
  s3bak status bin/s3bak               # status of a single sub-path

  # diff: content diff between backup and local
  s3bak diff bin                       # diff the whole entry
  s3bak diff bin/s3bak                 # single-file diff, CWD-relative local path
  s3bak diff ~/bin/s3bak               # single-file diff with ~ expansion

  # list: locally configured entries (no S3 access)
  s3bak list

  # ls-remote: what is on S3
  s3bak ls-remote                      # list entries stored on S3
  s3bak ls-remote bin                  # list files recorded in bin's manifest
  s3bak ls-remote bin/subdir           # list manifest lines under a sub-path
"""
    sys.stderr.write(text)
    sys.exit(status)


# =============================================================================
# Argument resolution
# =============================================================================


def _resolve_one_arg(cfg: Config, arg: str) -> tuple[str, str | None]:
    # No path separator: match strictly as an entry name.
    # Otherwise: treat as a local path made absolute against CWD/HOME, then find
    #   which entry's path contains it, preferring the longest prefix. Separators
    #   are platform-aware so native Windows "C:\dir\f" is a path, not a name.
    seps = [os.sep, os.altsep] if os.altsep else [os.sep]
    if not (any(s in arg for s in seps) or os.path.isabs(arg)):
        if arg in cfg.entries:
            return arg, None
        die(f"no such entry: {arg}")

    local = normalize_local_path(arg)
    best_name: str | None = None
    best_path: str = ""
    best_file: str | None = None
    for name, entry_cfg in cfg.entries.items():
        raw_path: str = entry_cfg["path"]
        entry_path = normalize_local_path(raw_path)
        if local == entry_path:
            candidate_file: str | None = None
        elif local.startswith(entry_path + os.sep):
            # The sub-path doubles as an S3 key fragment and a manifest rel,
            # both '/'-separated - normalize away a native Windows os.sep.
            candidate_file = local[len(entry_path) + 1 :]
            if os.sep != "/":
                candidate_file = candidate_file.replace(os.sep, "/")
        else:
            continue
        if best_name is None or len(entry_path) > len(best_path):
            best_name = name
            best_path = entry_path
            best_file = candidate_file

    if best_name is None:
        die(f"no such entry for path: {arg}")
    return best_name, best_file


def resolve_entry_file(cfg: Config, positional: list[str], cmd: str) -> tuple[str, str | None]:
    if len(positional) != 1:
        die(f"{cmd} takes <entry> or <path>")
    return _resolve_one_arg(cfg, positional[0])


def resolve_entry_files(
    cfg: Config, positional: list[str], cmd: str
) -> list[tuple[str, str | None]]:
    if not positional:
        die(f"{cmd} requires at least one entry or path")
    return [_resolve_one_arg(cfg, arg) for arg in positional]


# =============================================================================
# Main
# =============================================================================


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print_usage()

    subcmd = args[0]
    if subcmd in ("help", "-h", "--help"):
        print_usage(0)

    cfg = load_config()

    opt_all = False
    opt_dryrun = False
    opt_delete = False
    opt_meta_only = False
    opt_data_only = False
    opt_verbose = False
    opt_checksum = False
    opt_mtime_window: float | None = None
    opt_outpath: str | None = None
    opt_color: str = "auto"
    positional: list[str] = []

    def take_value(flag: str, idx: int) -> tuple[str, int]:
        # Support both --flag=value and --flag value
        if "=" in flag:
            return flag.split("=", 1)[1], idx
        if idx + 1 >= len(args):
            die(f"{flag} requires a value")
        return args[idx + 1], idx + 1

    i = 1
    while i < len(args):
        a = args[i]
        if a == "--all":
            opt_all = True
        elif a == "--dry-run":
            opt_dryrun = True
        elif a == "--delete":
            opt_delete = True
        elif a == "--meta-only":
            opt_meta_only = True
        elif a == "--data-only":
            opt_data_only = True
        elif a in ("-v", "--verbose"):
            opt_verbose = True
        elif a == "--checksum":
            opt_checksum = True
        elif a == "--mtime-window" or a.startswith("--mtime-window="):
            val, i = take_value(a, i)
            try:
                opt_mtime_window = float(val)
            except ValueError:
                die(f"--mtime-window requires a non-negative number of seconds (got {val!r})")
            if opt_mtime_window < 0:
                die(f"--mtime-window must be >= 0 (got {opt_mtime_window})")
        elif a in ("-o", "--output", "--outpath") or a.startswith(("--output=", "--outpath=")):
            opt_outpath, i = take_value(a, i)
        elif a == "--color":
            opt_color = "always"
        elif a.startswith("--color="):
            val = a.split("=", 1)[1]
            if val not in ("auto", "always", "never"):
                die(f"invalid --color value: {val} (use auto|always|never)")
            opt_color = val
        elif a == "--no-color":
            opt_color = "never"
        elif a in ("-h", "--help"):
            print_usage(0)
        elif a == "--":
            positional.extend(args[i + 1 :])
            break
        elif a.startswith("-"):
            die(f"unknown option: {a}")
        else:
            positional.append(a)
        i += 1

    opts = Opts(
        dryrun=opt_dryrun,
        delete=opt_delete,
        meta_only=opt_meta_only,
        data_only=opt_data_only,
        verbose=opt_verbose,
        checksum=opt_checksum,
        outpath=opt_outpath,
        color=opt_color,
    )

    # Global option/command compatibility. Rejecting an inapplicable flag here
    # (rather than silently ignoring it) matters most for --dry-run: `pull
    # --dry-run` would otherwise perform a REAL restore the user believed was
    # a preview.
    if opt_all and positional:
        die("--all cannot be combined with explicit entries")
    if opt_all and subcmd not in ("push", "pull", "status"):
        die(f"{subcmd} does not support --all")

    if opt_meta_only and opt_data_only:
        die("--meta-only and --data-only are mutually exclusive")
    if (opt_meta_only or opt_data_only) and subcmd not in ("push", "pull"):
        flag = "--meta-only" if opt_meta_only else "--data-only"
        die(f"{flag} only applies to push and pull")

    if opt_dryrun and subcmd != "push":
        die("--dry-run only applies to push")

    if opt_delete and subcmd not in ("push", "pull"):
        die("--delete only applies to push and pull (pull removes local extras)")

    if opt_checksum and subcmd not in ("push", "pull"):
        die("--checksum only applies to push and pull")

    if opt_mtime_window is not None and subcmd not in ("push", "pull", "status"):
        die("--mtime-window only applies to push, pull, and status")

    if opt_outpath is not None and subcmd != "pull":
        die("-o/--output only applies to pull")

    # A CLI --mtime-window overrides both the top-level and per-entry config
    # windows for this run (0 = exact). Affects the size+mtime check shared
    # by push / pull / status (see Config.window_for).
    if opt_mtime_window is not None:
        cfg.mtime_window_override = opt_mtime_window

    if subcmd == "push":
        if opt_all:
            entries = sorted(cfg.entries.keys())
            sub_by_entry: dict[str, str | None] = {e: None for e in entries}
        else:
            resolved = resolve_entry_files(cfg, positional, "push")
            seen: set[str] = set()
            for e, _s in resolved:
                if e in seen:
                    die(
                        f"duplicate entry in push: {e} "
                        f"(parallel push of the same entry is not supported)"
                    )
                seen.add(e)
            entries = [e for e, _ in resolved]
            sub_by_entry = {e: s for e, s in resolved}

        def _push_one(cfg_: Config, entry_: str, opts_: Opts) -> int:
            return cmd_push(cfg_, entry_, opts_, sub=sub_by_entry.get(entry_))

        return run_entries(_push_one, cfg, entries, opts)

    elif subcmd == "pull":
        if opt_all:
            if opts.outpath:
                die("--all cannot be combined with -o/--output")
            return run_entries(cmd_pull, cfg, sorted(cfg.entries.keys()), opts)
        entry, sub = resolve_entry_file(cfg, positional, "pull")
        return cmd_pull(cfg, entry, opts, sub=sub)

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
                    die(f"conflicting sub paths for entry {e}")
                status_sub_by_entry[e] = s

        def _status_one(cfg_: Config, entry_: str, opts_: Opts) -> int:
            return cmd_status(cfg_, entry_, opts_, sub=status_sub_by_entry.get(entry_))

        return run_entries(_status_one, cfg, entries, opts)

    elif subcmd == "diff":
        entry, file = resolve_entry_file(cfg, positional, "diff")
        return cmd_diff(cfg, entry, opts, file)

    elif subcmd == "show":
        entry, file = resolve_entry_file(cfg, positional, "show")
        return cmd_show(cfg, entry, opts, file)

    elif subcmd == "list":
        if positional:
            die("list takes no arguments (use 'ls-remote <entry>' for manifest contents)")
        return cmd_list(cfg, opts)

    elif subcmd == "ls-remote":
        if len(positional) > 1:
            die("ls-remote takes at most one entry or path")
        if not positional:
            return cmd_ls_remote(cfg, opts, None, None)
        entry, sub = _resolve_one_arg(cfg, positional[0])
        return cmd_ls_remote(cfg, opts, entry, sub)

    else:
        err(f"unknown command: {subcmd}")
        print_usage()


def _sdk_errors() -> tuple[type[BaseException], ...]:
    """The boto3-s3 / botocore error types, imported lazily so `help` / `list`
    stay SDK-free (the except clause only evaluates this on an error). Returns an
    empty tuple if the SDK is unimportable, so matching never masks the original
    error with an ImportError."""
    try:
        from boto3_s3 import Boto3S3Error
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        return ()
    return (Boto3S3Error, BotoCoreError, ClientError)


def run() -> int:
    """Console entry point: install signal handling and translate exceptions
    into exit codes. This is what the ``s3bak`` command invokes."""
    reset_warnings()
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
    try:
        rc = main() or 0
    except subprocess.CalledProcessError as e:
        cmd_str = shlex.join(e.cmd) if isinstance(e.cmd, list) else str(e.cmd)
        err(f"command failed: {cmd_str}")
        return e.returncode or 1
    except BrokenPipeError:
        return 141
    except FileNotFoundError as e:
        # An external command is not on PATH (the `diff` binary), or a required
        # file vanished mid-run: report cleanly instead of a raw traceback.
        err(str(e))
        return 1
    except manifest.ManifestError as e:
        err(str(e))
        return 1
    except _sdk_errors() as e:
        err(str(e))
        return 1
    # A run that only warned (skipped files etc.) but hit no hard error exits 2
    # (aws-style), after the manifest update has completed.
    if rc == 0 and warning_count() > 0:
        return 2
    return rc


if __name__ == "__main__":
    sys.exit(run())
