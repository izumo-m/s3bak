# s3bak configuration example.
#
# Copy this to ~/.config/s3bak/config.py (or point $S3BAK_CONFIG at it) and
# edit the values below. It is plain Python, executed by s3bak at startup, so
# you can build paths and add entries however you like.
#
# Note: entry paths are used as-is - "~" is NOT expanded. Build them from HOME
# (as below) or use absolute paths.

import os

# Fail loudly when HOME is unset: a silent "" fallback would turn f"{HOME}/bin"
# into "/bin" and aim the backup at a system directory. On Windows build from
# USERPROFILE instead: HOME = os.environ["USERPROFILE"]
HOME = os.environ["HOME"]

# AWS profile used for S3 access (required); read by boto3 / boto3-s3.
profile = "default"

# Destination root on S3 (required). Must start with "s3://".
prefix = "s3://my-bucket/backup"

# Tuning (all optional). s3bak does not read aws-cli's [s3] settings, so set
# these here if the defaults don't fit.
#
#   max_concurrency    parallel S3 transfer threads for cp / sync (default 10),
#                      like aws-cli's s3.max_concurrent_requests. Also sizes
#                      verify --checksum's local hashing pool. (The push/pull
#                      sync compare itself runs serially: push's journal needs
#                      its decisions in key order.)
#   entry_concurrency  how many entries run at once in a multi-entry command
#                      (default: 4). Each entry also opens its own transfer
#                      pool (max_concurrency threads, default ~10), so 4
#                      entries already means around 40 transfers in flight;
#                      set this explicitly to raise or lower that ceiling.
#   mtime_window       size+mtime-check tolerance in seconds (fractional ok,
#                      default 0.01 = 10ms, 0 = exact st_mtime_ns match). The
#                      default absorbs the rounding of NTFS (100ns) and exFAT
#                      (10ms) so a pull onto them cannot re-download an unchanged
#                      file forever; a filesystem with coarser timestamps needs
#                      a larger value here: FAT32 2s, HFS+ 1s, WSL2 drvfs
#                      (/mnt/c) 1s (it truncates utime writes to whole seconds
#                      even though it reads NTFS mtimes at 100ns). Overridable
#                      per entry.
#
# max_concurrency = 10
# entry_concurrency = 4
# mtime_window = 0.01

# Directories / files to back up (required), keyed by entry name.
#
# Per-entry keys:
#   path          (required) absolute local path to back up ("~" is not
#                 expanded; build from HOME as above)
#   excludes      (optional) glob patterns excluded from the sync (aws s3-style)
#   pre_hook      (optional) argument list run before the entry is pushed
#   post_hook     (optional) argument list run after a push that did work
#   mtime_window  (optional) overrides the top-level mtime_window for this entry
#                 (0 = exact); the CLI --mtime-window overrides both
#
# Hooks are executed directly without a command shell. The first item names the
# executable and each remaining item is passed as one argument. Shell syntax
# such as globbing, pipelines, and redirection is not interpreted; put complex
# work in a standalone executable or script.
#
# When the push that fires post_hook was journal-driven (an ordinary
# directory push, or a sub-path push whose target still exists), post_hook
# also gets S3BAK_JOURNAL in its environment: the path of that push's journal
# file (see docs/journal.md), readable only until the hook returns. It is
# unset for a single-file entry, a sub-path deletion, and the on-demand
# "s3bak hook" command - a hook must treat "unset" as "no per-file detail,
# assume anything may have changed".
entries = {
    ".ssh": {"path": f"{HOME}/.ssh", "excludes": ["agent/*"]},
    "bin": {"path": f"{HOME}/bin", "excludes": ["__pycache__/*"], "mtime_window": 0},
    ".emacs.d": {
        "path": f"{HOME}/.emacs.d",
        "excludes": ["*.elc", "elpa/*", "eln-cache/*"],
    },
    # Absolute paths work too (no HOME needed):
    "wsl.conf": {"path": "/etc/wsl.conf"},
    "vault": {
        "path": "/mnt/data/vault",
        "post_hook": ["rclone", "copy", "/mnt/data/vault", "remote:vault"],
    },
}

# Named sets of entry names (optional). A group can be typed wherever a
# command takes several entries - push, pull, status, verify, hook - and is
# replaced by its entries before the command runs, so no group name ever
# reaches S3. (hook is the exception: a named group runs only the members
# that configure the hook it was asked for.) A group may name another group,
# and one entry may belong to several groups. A group name must not be an
# entry name, and a group has no path of its own: s3bak rejects
# "dotfiles/bin", and rejects a group where diff, show or ls-remote expects
# a single target.
#
# groups = {
#     "dotfiles": [".ssh", "bin", ".emacs.d"],
#     "nightly": ["dotfiles", "vault"],
# }

# config.py is plain Python, so entries can be added conditionally, e.g. per
# host or platform:
#
#     import socket
#     if socket.gethostname() == "myhost":
#         entries["work"] = {"path": f"{HOME}/work"}
