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
import shlex
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
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
)
from s3bak.compare import _resolve_use_color
from s3bak.config import Config, Opts, load_config
from s3bak.confirm import reset_confirmations
from s3bak.console import (
    die,
    err,
    expand_home,
    normalize_local_path,
    reset_warnings,
    warning_count,
)
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

    statuses = [0] * len(entries)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fn, cfg, entry, opts): index for index, entry in enumerate(entries)
        }
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            try:
                statuses[index] = future.result()
            except Exception as exc:
                err(f"{entries[index]}: {exc}")
                statuses[index] = 1
    # Completion order varies with scheduling. Preserve the first configured
    # entry's failure so --all has a deterministic exit code (including a
    # post_hook's documented 3+ code) rather than whichever worker happened to
    # finish first.
    return next((status for status in statuses if status), 0)


def _validate_distinct_entries(resolved: Sequence[tuple[str, str | None]], command: str) -> None:
    seen: set[str] = set()
    for entry, _sub in resolved:
        if entry in seen:
            die(
                f"duplicate entry in {command}: {entry} "
                f"(parallel {command} of the same entry is not supported)"
            )
        seen.add(entry)


def _run_resolved_entries(
    fn: Callable[[Config, str, Opts, str | None], int],
    cfg: Config,
    resolved: Sequence[tuple[str, str | None]],
    opts: Opts,
) -> int:
    entries = [entry for entry, _sub in resolved]
    sub_by_entry = {entry: sub for entry, sub in resolved}

    def _run_one(cfg_: Config, entry_: str, opts_: Opts) -> int:
        return fn(cfg_, entry_, opts_, sub_by_entry.get(entry_))

    return run_entries(_run_one, cfg, entries, opts)


# =============================================================================
# Usage
# =============================================================================


def print_usage(status: int = 1) -> NoReturn:
    config_path = os.environ.get("S3BAK_CONFIG") or expand_home("~/.config/s3bak/config.py")
    text = f"""\
Usage: s3bak <command> [options] [args]

Commands:
  push <entry|path>...         Back up entries or sub-paths to S3
  pull <entry|path>...         Restore entries or sub-paths (use --all for every entry)
  show <entry|path>            Print a single file from the backup to stdout
  status <entry|path>...       Compare local vs backup (metadata only)
  diff <entry|path>            Show content diff between backup and local
  list                         List locally configured entries
  ls-remote [entry|path]       List S3 entries, or files under an entry/sub-path

Options:
  --all            Apply the command to all configured entries
  --dry-run        Show what would happen without changing anything (push/pull)
  --delete         Delete extras, confirming each one (y/n/a/d/q): push removes
                   S3 objects with no local counterpart, pull removes local
                   files not in the backup. Without a TTY every answer is no
  --yes            Answer yes to every --delete confirmation (unattended mirror)
  --meta-only      Sync only metadata (the manifest), skip file data (push/pull)
  --data-only      Sync only file data, leave manifest/local-meta untouched (push/pull)
  --checksum       Compare by content (ETag) instead of the manifest size+mtime
                   check; reads every candidate file (push/pull)
  --mtime-window <seconds>  Override config's size+mtime-check tolerance for
                   this run (fractional ok, 0 = exact); affects push/pull/status
  -o, --output <path>  Restore destination for a single pull target
                       (default: the entry's configured path)
  -v, --verbose    Verbose output (details per field in status)
  --color[=WHEN]   Colorize status (verbose) and diff output
                   (WHEN: auto|always|never; default auto).
                   --color alone == --color=always. Honors NO_COLOR env var.
  --no-color       Disable color (same as --color=never)
  --version        Show the program version and exit
  --help           Show this help

status letters (push-oriented: what would change on the backup):
  M <path>         modified (metadata differs between local and backup)
  A <path>         only locally, not in backup   (push would add)
  D <path>         only in backup, not locally   (push --delete would remove)

Config file: {config_path}

Examples:
  # push: back up one or more entries (or sub-paths)
  s3bak push bin .bash.d               # push selected entries
  s3bak push --all                     # push every configured entry
  s3bak push --all --dry-run           # preview without uploading
  s3bak push --meta-only bin           # upload metadata (the manifest) only
  s3bak push --meta-only --all         # upload metadata for all entries
  s3bak push --data-only bin           # upload data only, leave manifest unchanged
  s3bak push bin --delete              # remove S3 orphans, confirming each
  s3bak push --all --delete --yes      # unattended mirror (e.g. cron)
  s3bak push bin/s3bak                 # single file inside the bin entry
  s3bak push ~/bin/s3bak               # same, via ~ expansion
  s3bak push bin/subdir                # only the sub-directory

  # pull: restore one or more entries/sub-paths (use --all for every entry)
  s3bak pull bin                       # restore to the configured path
  s3bak pull bin home-docs             # restore selected entries in parallel
  s3bak pull bin -o /tmp/restore       # restore to an alternative path
  s3bak pull bin --delete              # also remove local files not in backup
  s3bak pull bin --delete --dry-run    # preview a mirror restore
  s3bak pull --all                     # restore every entry in parallel
  s3bak pull --meta-only bin           # restore metadata only (no file download)
  s3bak pull --data-only bin           # restore file data only (no mode/mtime applied)
  s3bak pull bin/s3bak                 # restore a single file
  s3bak pull bin/subdir -o /tmp/restore # restore a sub-tree elsewhere

  # show: print a single backed-up file to stdout
  s3bak show wsl.conf                  # single-file entry (no slash = entry name)
  s3bak show bin/s3bak                 # entry-rooted path, independent of CWD
  s3bak show ~/bin/s3bak               # local path with ~ expansion
  s3bak show /home/me/bin/s3bak | less # absolute local path

  # status: compare local vs backup (metadata only, both directions)
  s3bak status bin                     # M/A/D summary for one entry
  s3bak status --all                   # status of every entry
  s3bak status -v bin                  # verbose per-field differences
  s3bak status bin/s3bak               # status of a single sub-path

  # diff: content diff between backup and local
  s3bak diff bin                       # diff the whole entry
  s3bak diff bin/s3bak                 # entry-rooted path, independent of CWD
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
    # A bare name is an entry. ``entry/sub`` is entry-rooted syntax independent
    # of CWD; every other path is resolved locally and matched to the containing
    # configured entry (longest root wins).
    seps = [os.sep, os.altsep] if os.altsep else [os.sep]
    if not (any(s in arg for s in seps) or os.path.isabs(arg)):
        if arg in cfg.entries:
            return arg, None
        die(f"no such entry: {arg}")

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
            if sub == ".." or sub.startswith("../") or sub.startswith("/"):
                die(f"sub path must stay inside entry {name}: {arg}")
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
        die(f"no such entry for path: {arg}")
    longest = max(length for length, _name, _sub in matches)
    best = [(name, sub) for length, name, sub in matches if length == longest]
    if len(best) > 1:
        names = ", ".join(sorted(name for name, _sub in best))
        die(f"path is ambiguous between entries {names}: {arg}")
    return best[0]


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
                die(
                    "pull restore destinations overlap: "
                    f"{left_entry} ({left_path}) and {right_entry} ({right_path})"
                )


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
    commands = {"push", "pull", "show", "status", "diff", "list", "ls-remote"}
    if subcmd not in commands:
        err(f"unknown command: {subcmd}")
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
        elif a == "--yes":
            opt_yes = True
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
            if not math.isfinite(opt_mtime_window) or opt_mtime_window < 0:
                die(f"--mtime-window must be >= 0 (got {opt_mtime_window})")
        elif a in ("-o", "--output", "--outpath") or a.startswith(("--output=", "--outpath=")):
            opt_outpath, i = take_value(a, i)
            if "=" not in a and opt_outpath.startswith("-"):
                die(f"{a} requires a path value (use --output=<path> for a path starting with '-')")
        elif a == "--color":
            opt_color = "always"
        elif a.startswith("--color="):
            val = a.split("=", 1)[1]
            if val not in ("auto", "always", "never"):
                die(f"invalid --color value: {val} (use auto|always|never)")
            opt_color = val
        elif a == "--no-color":
            opt_color = "never"
        elif a == "--help":
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
        die("--all cannot be combined with explicit entries")
    if opt_all and subcmd not in ("push", "pull", "status"):
        die(f"{subcmd} does not support --all")

    if opt_meta_only and opt_data_only:
        die("--meta-only and --data-only are mutually exclusive")
    if (opt_meta_only or opt_data_only) and subcmd not in ("push", "pull"):
        flag = "--meta-only" if opt_meta_only else "--data-only"
        die(f"{flag} only applies to push and pull")

    if opt_dryrun and subcmd not in ("push", "pull"):
        die("--dry-run only applies to push and pull")

    if opt_delete and subcmd not in ("push", "pull"):
        die("--delete only applies to push and pull")
    if opt_yes and not opt_delete:
        die("--yes requires --delete (it answers deletion confirmations)")
    if subcmd == "push" and opt_delete and opt_meta_only:
        die("push --delete cannot be combined with --meta-only (a deletion drops the object too)")
    if subcmd == "push" and opt_delete and opt_data_only:
        die("push --delete cannot be combined with --data-only (a deletion drops the record too)")

    if opt_checksum and subcmd not in ("push", "pull"):
        die("--checksum only applies to push and pull")
    if opt_checksum and opt_meta_only:
        die("--checksum cannot be combined with --meta-only (no file data is compared)")
    if opt_checksum and opt_mtime_window is not None:
        die("--mtime-window cannot be combined with --checksum (content comparison ignores it)")
    if subcmd == "pull" and opt_delete and opt_meta_only:
        die("pull --delete cannot be combined with --meta-only")

    if opt_mtime_window is not None and subcmd not in ("push", "pull", "status"):
        die("--mtime-window only applies to push, pull, and status")

    if opt_outpath is not None and subcmd != "pull":
        die("-o/--output only applies to pull")
    if opt_outpath == "":
        die("-o/--output requires a non-empty path")
    if subcmd == "pull" and opt_outpath is not None:
        if opt_all:
            die("--all cannot be combined with -o/--output")
        if len(positional) > 1:
            die("-o/--output cannot be combined with multiple pull targets")

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
        return _run_resolved_entries(cmd_push, cfg, resolved, opts)

    elif subcmd == "pull":
        if opt_all:
            resolved = [(entry, None) for entry in sorted(cfg.entries.keys())]
        else:
            resolved = resolve_entry_files(cfg, positional, "pull")
        _validate_distinct_entries(resolved, "pull")
        _validate_pull_destinations(cfg, resolved)
        return _run_resolved_entries(cmd_pull, cfg, resolved, opts)

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
            entries = list(status_sub_by_entry)

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
    except OSError as e:
        # Permission errors, disk-full failures, and local I/O races are normal
        # operational failures for a backup tool, not reasons to expose a Python
        # traceback to the CLI user.
        err(str(e))
        return 1
    except manifest.ManifestError as e:
        err(str(e))
        return 1
    except _sdk_errors() as e:
        err(str(e))
        return 1
    # A run that only warned (skipped files etc.) but hit no hard error exits 2
    # (aws-style). Successful work is retained, but callers must inspect it.
    if rc == 0 and warning_count() > 0:
        return 2
    return rc


if __name__ == "__main__":
    sys.exit(run())
