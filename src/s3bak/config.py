# Requires Python 3.10+
"""Runtime config (``config.py`` loading) and per-invocation options.

``load_config`` reads the user's ``~/.config/s3bak/config.py`` (plain Python),
validates it, and attaches a ready ``Boto3S3Store`` for S3 commands. ``Config``
and ``Opts`` are the two values threaded through every command.
"""

from __future__ import annotations

import math
import os
import tokenize
from dataclasses import dataclass
from typing import Any

from s3bak.console import die, expand_home
from s3bak.manifest import MANIFEST_SUFFIX
from s3bak.store import Boto3S3Store

# Default mtime window for the size+mtime check (seconds). 2s absorbs every common
# restored mtime. 10 ms absorbs the rounding of the common modern filesystems -
# NTFS's 100 ns and exFAT's 10 ms - so a pull onto them cannot loop re-downloading
# an unchanged file. Coarser filesystems (FAT32 2 s, HFS+ 1 s) need a larger
# value set in config.py. Seconds, fractional allowed (0 = exact st_mtime_ns).
DEFAULT_MTIME_WINDOW = 0.01


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
    # Top-level mtime tolerance for the size+mtime check, in seconds (0 = exact st_mtime_ns
    # match). An entry may override it with a per-entry `mtime_window`, and the
    # CLI --mtime-window overrides both (see window_for).
    mtime_window: float = DEFAULT_MTIME_WINDOW
    mtime_window_override: float | None = None  # set by CLI --mtime-window
    store: Boto3S3Store | None = None

    def window_for(self, entry: str) -> float:
        """Effective mtime window (seconds) for `entry`'s size+mtime check:
        CLI override > per-entry `mtime_window` > top-level `mtime_window`."""
        if self.mtime_window_override is not None:
            return float(self.mtime_window_override)
        entry_cfg = self.entries.get(entry)
        if entry_cfg is not None:
            per_entry = entry_cfg.get("mtime_window")
            if per_entry is not None:
                return float(per_entry)
        return float(self.mtime_window)

    def window_ns_for(self, entry: str) -> int:
        return round(self.window_for(entry) * 1_000_000_000)


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


def _config_seconds(value: Any, config_path: str, *, label: str) -> float | None:
    """Validate an optional non-negative duration in seconds (int or float,
    fractional allowed; bool rejected). Returns None when unset."""
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        die(f"{label} must be a non-negative number of seconds in {config_path} (got {value!r})")
    return float(value)


def _validate_entry_name(name: Any, config_path: str) -> str:
    if not isinstance(name, str) or not name:
        die(f"entry names must be non-empty strings in {config_path} (got {name!r})")
    if name in (".", "..") or "/" in name or "\\" in name:
        die(f"entry name must be one path component in {config_path} (got {name!r})")
    if name.endswith(MANIFEST_SUFFIX):
        die(f"entry name must not end with {MANIFEST_SUFFIX!r} in {config_path} (got {name!r})")
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        die(f"entry name must not contain control characters in {config_path} (got {name!r})")
    return name


def load_config(*, create_store: bool = True) -> Config:
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

    ns: dict[str, Any] = {"__file__": config_path, "__name__": "s3bak_config"}
    try:
        with tokenize.open(config_path) as f:
            code = f.read()
        exec(compile(code, config_path, "exec"), ns)
    except Exception as e:
        die(f"error loading {config_path}: {e}")

    profile = ns.get("profile")
    prefix = ns.get("prefix")
    entries = ns.get("entries")

    if not isinstance(profile, str) or not profile or not isinstance(prefix, str) or not prefix:
        die(f"profile and prefix must be set in {config_path}")
    if not isinstance(entries, dict) or not entries:
        die(f"no entries defined in {config_path}")
    if not prefix.startswith("s3://"):
        die(f"prefix must start with s3:// (got '{prefix}')")

    rest = prefix[5:]
    bucket = rest.split("/", 1)[0]
    path_prefix = rest.split("/", 1)[1].strip("/") if "/" in rest else ""

    if not bucket:
        die(f"could not parse bucket from prefix='{prefix}'")
    # Keep direct boto3 keys and boto3-s3 URLs on the same canonical prefix.
    # A user-friendly trailing slash must not become a doubled slash in transfer
    # URLs while direct GetObject/PutObject use the stripped path_prefix.
    prefix = f"s3://{bucket}" + (f"/{path_prefix}" if path_prefix else "")

    # Optional knobs (see config.example.py):
    #   max_concurrency   - parallel S3 transfer threads for cp / sync
    #   compare_workers   - parallel ETag comparisons under --checksum
    #   entry_concurrency - entries processed at once under --all
    #   mtime_window      - mtime tolerance for the size+mtime check, seconds (0 = exact),
    #                       top-level default; overridable per entry
    max_concurrency = _config_int(ns, "max_concurrency", config_path, minimum=1)
    compare_workers = _config_int(ns, "compare_workers", config_path, minimum=1)
    entry_concurrency = _config_int(ns, "entry_concurrency", config_path, minimum=1)
    mtime_window = _config_seconds(ns.get("mtime_window"), config_path, label="mtime_window")

    # Per-entry validation: every command dereferences entry_cfg["path"], so a
    # malformed entry must die with a message, not a KeyError traceback.
    for raw_name, entry_cfg in entries.items():
        name = _validate_entry_name(raw_name, config_path)
        if not isinstance(entry_cfg, dict):
            die(f"entries[{name!r}] must be a dict in {config_path} (got {entry_cfg!r})")
        path = entry_cfg.get("path")
        if not isinstance(path, str) or not path:
            die(f"entries[{name!r}].path must be a non-empty string in {config_path}")
        excludes = entry_cfg.get("excludes")
        if excludes is not None and (
            not isinstance(excludes, list) or not all(isinstance(x, str) for x in excludes)
        ):
            die(f"entries[{name!r}].excludes must be a list of strings in {config_path}")
        for hook in ("pre_hook", "post_hook"):
            hook_value = entry_cfg.get(hook)
            if hook_value is not None and (
                not isinstance(hook_value, list)
                or not hook_value
                or not all(isinstance(arg, str) for arg in hook_value)
                or not hook_value[0]
            ):
                die(
                    f"entries[{name!r}].{hook} must be a non-empty list of strings "
                    f"with a non-empty executable in {config_path}"
                )
        # Per-entry mtime_window overrides the top-level one (validated the same way).
        _config_seconds(
            entry_cfg.get("mtime_window"), config_path, label=f"entries[{name!r}].mtime_window"
        )

    cfg = Config(
        profile=profile,
        prefix=prefix,
        bucket=bucket,
        path_prefix=path_prefix,
        entries=entries,
        entry_concurrency=entry_concurrency,
        mtime_window=DEFAULT_MTIME_WINDOW if mtime_window is None else mtime_window,
    )
    if create_store:
        cfg.store = Boto3S3Store(
            profile,
            prefix,
            bucket,
            path_prefix,
            max_concurrency=max_concurrency,
            compare_workers=compare_workers,
        )
    return cfg
