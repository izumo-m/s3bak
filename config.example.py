# s3bak configuration example.
#
# Copy this to ~/.config/s3bak/config.py (or point $S3BAK_CONFIG at it) and
# edit the values below. It is plain Python, executed by s3bak at startup, so
# you can build paths and add entries however you like.
#
# Note: entry paths are used as-is - "~" is NOT expanded. Build them from HOME
# (as below) or use absolute paths.

import os

HOME = os.environ.get("HOME", "")

# AWS profile used for S3 access (required); read by boto3 / boto3-s3.
profile = "default"

# Destination root on S3 (required). Must start with "s3://".
prefix = "s3://my-bucket/backup"

# Tuning (all optional). s3bak does not read aws-cli's [s3] settings, so set
# these here if the defaults don't fit.
#
#   max_concurrency    parallel S3 transfer threads for cp / sync (default 10),
#                      like aws-cli's s3.max_concurrent_requests.
#   compare_workers    parallel ETag comparisons under --checksum, which
#                      hashes each candidate file locally; raise this to speed
#                      up a --checksum sync bottlenecked on that hashing,
#                      lower it to cap CPU/IO. Defaults to max_concurrency,
#                      else 10. The default (non-checksum) compare is
#                      stat-only and needs no workers.
#   entry_concurrency  how many entries run at once in a multi-entry command
#                      (default: all of them, one thread each). Each entry also
#                      opens its own transfer pool, so cap this when you have
#                      many entries to bound the total thread count.
#   mtime_window       size+mtime-check tolerance in seconds (fractional ok,
#                      default 0.01 = 10ms, 0 = exact st_mtime_ns match). The
#                      default absorbs the rounding of NTFS (100ns) and exFAT
#                      (10ms) so a pull onto them cannot re-download an unchanged
#                      file forever; a coarser filesystem (FAT32 2s, HFS+ 1s)
#                      needs a larger value here. Overridable per entry.
#
# max_concurrency = 10
# compare_workers = 10
# entry_concurrency = 4
# mtime_window = 0.01

# Directories / files to back up (required), keyed by entry name.
#
# Per-entry keys:
#   path          (required) local path to back up (build from HOME or absolute)
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

# config.py is plain Python, so entries can be added conditionally, e.g. per
# host or platform:
#
#     import socket
#     if socket.gethostname() == "myhost":
#         entries["work"] = {"path": f"{HOME}/work"}
