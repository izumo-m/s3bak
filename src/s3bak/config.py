# Requires Python 3.10+
"""Runtime config (``config.py`` loading) and per-invocation options.

``load_config`` reads the user's ``~/.config/s3bak/config.py`` (plain Python),
validates it, and attaches a ready ``Boto3S3Store``. ``Config`` and ``Opts``
are the two values threaded through every command.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from s3bak.console import die, err, expand_home
from s3bak.store import Boto3S3Store

# Default quick-check mtime window (seconds). 2s absorbs every common
# filesystem's mtime granularity (FAT 2s, exFAT 10ms, NTFS 100ns), so a pull
# onto a coarser filesystem cannot loop on an unrepresentable restored mtime.
DEFAULT_MTIME_WINDOW = 2


@dataclass
class Config:
    profile: str
    prefix: str
    bucket: str
    path_prefix: str
    entries: dict[str, dict[str, Any]]
    # Max entries processed at once under --all (None = one thread per entry,
    # i.e. all at once). Consumed by run_entries, not the store.
    entry_concurrency: int | None = None
    # Top-level quick-check mtime tolerance in seconds (0 = exact st_mtime_ns
    # match). An entry may override it with a per-entry `mtime_window`, and the
    # CLI --mtime-window overrides both (see window_for).
    mtime_window: int = DEFAULT_MTIME_WINDOW
    mtime_window_override: int | None = None  # set by CLI --mtime-window
    store: Boto3S3Store | None = None

    def window_for(self, entry: str) -> int:
        """Effective quick-check mtime window (seconds) for `entry`:
        CLI override > per-entry `mtime_window` > top-level `mtime_window`."""
        if self.mtime_window_override is not None:
            return self.mtime_window_override
        entry_cfg = self.entries.get(entry)
        if entry_cfg is not None:
            per_entry = entry_cfg.get("mtime_window")
            if per_entry is not None:
                return per_entry
        return self.mtime_window

    def window_ns_for(self, entry: str) -> int:
        return self.window_for(entry) * 1_000_000_000


@dataclass
class Opts:
    dryrun: bool = False
    delete: bool = False
    meta_only: bool = False
    data_only: bool = False
    verbose: bool = False
    checksum: bool = False
    outpath: str | None = None
    color: str = "auto"


def _config_int(
    ns: dict[str, Any], name: str, config_path: str, *, minimum: int, label: str = ""
) -> int | None:
    """Read an optional integer setting from the config namespace `ns`.

    Returns None when unset; dies with a clear message on a non-int (bool
    included, since `True` is an int in Python) or a value below `minimum`.
    `label` prefixes the setting name in the error (e.g. an entry name).
    """
    value = ns.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        kind = "a positive integer" if minimum > 0 else "a non-negative integer"
        where = f"{label}.{name}" if label else name
        die(f"{where} must be {kind} in {config_path} (got {value!r})")
    return value


def load_config() -> Config:
    config_path = os.environ.get("S3BAK_CONFIG")
    if not config_path:
        config_path = expand_home("~/.config/s3bak/config.py")

    if not os.path.isfile(config_path):
        config_sh = expand_home("~/.config/s3bak/config.sh")
        if os.path.isfile(config_sh):
            die(
                f"found {config_sh} but s3bak now requires config.py\n"
                f"  Please create: {config_path}"
            )
        die(
            f"config file not found: {config_path}\n\n"
            f"Create it with contents like:\n\n"
            f'  profile = "default"\n'
            f'  prefix  = "s3://my-bucket/backup"\n\n'
            f"  entries = {{\n"
            f'      "home-docs": {{"path": "/home/user/Documents"}},\n'
            f"  }}"
        )

    ns: dict[str, Any] = {}
    with open(config_path) as f:
        code = f.read()
    try:
        exec(compile(code, config_path, "exec"), ns)
    except Exception as e:
        die(f"error loading {config_path}: {e}")

    profile: str | None = ns.get("profile")
    prefix: str | None = ns.get("prefix")
    entries: dict[str, dict[str, Any]] | None = ns.get("entries")

    if not profile or not prefix:
        die(f"profile and prefix must be set in {config_path}")
    if not entries:
        die(f"no entries defined in {config_path}")
    if "all" in entries:
        err("warning: entry name 'all' conflicts with --all flag; consider renaming")
    if not prefix.startswith("s3://"):
        die(f"prefix must start with s3:// (got '{prefix}')")

    rest = prefix[5:]
    bucket = rest.split("/", 1)[0]
    path_prefix = rest.split("/", 1)[1].strip("/") if "/" in rest else ""

    if not bucket:
        die(f"could not parse bucket from prefix='{prefix}'")

    # Optional knobs (see config.example.py):
    #   max_concurrency   - parallel S3 transfer threads for cp / sync
    #   compare_workers   - parallel ETag comparisons under --checksum
    #   entry_concurrency - entries processed at once under --all
    #   mtime_window      - quick-check mtime tolerance in seconds (0 = exact),
    #                       top-level default; overridable per entry
    max_concurrency = _config_int(ns, "max_concurrency", config_path, minimum=1)
    compare_workers = _config_int(ns, "compare_workers", config_path, minimum=1)
    entry_concurrency = _config_int(ns, "entry_concurrency", config_path, minimum=1)
    mtime_window = _config_int(ns, "mtime_window", config_path, minimum=0)

    # Per-entry mtime_window overrides the top-level one (validated the same way).
    for name, entry_cfg in entries.items():
        _config_int(entry_cfg, "mtime_window", config_path, minimum=0, label=f"entries[{name!r}]")

    cfg = Config(
        profile=profile,
        prefix=prefix,
        bucket=bucket,
        path_prefix=path_prefix,
        entries=entries,
        entry_concurrency=entry_concurrency,
        mtime_window=DEFAULT_MTIME_WINDOW if mtime_window is None else mtime_window,
    )
    cfg.store = Boto3S3Store(
        profile,
        prefix,
        bucket,
        path_prefix,
        max_concurrency=max_concurrency,
        compare_workers=compare_workers,
    )
    return cfg
