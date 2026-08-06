# Requires Python 3.10+
"""Manifest-record vs local-filesystem comparison, plus the status/diff
presentation helpers (color, humanized sizes/durations).

``compare_to_local`` shares its size+mtime check with the sync's
``ManifestFilter`` (see manifest.py), so ``status`` and push/pull agree on
what counts as changed; ``mode_differs`` is the shared mode predicate, used
here for the metadata report and by push's manifest-refresh check.
"""

from __future__ import annotations

import datetime
import functools
import os
import stat as stat_mod
import subprocess
import sys
from dataclasses import dataclass

from s3bak.console import IS_WINDOWS
from s3bak.manifest import ManifestEntry

_ANSI_GREEN = "\033[1;32m"
_ANSI_RESET = "\033[0m"

# Whether the platform's os.utime can set a symlink's own mtime without
# following it (POSIX; not Windows). Gates every place a symlink's mtime is
# compared or restored - compare_to_stat's symlink branch, PushJournal's
# symlink journaling, and restore's _place_symlink - so an unsupported
# platform degrades all three to target-only handling instead of churning the
# manifest with a creation-time mtime on every pull-then-push cycle.
SYMLINK_MTIME_SUPPORTED = os.utime in os.supports_follow_symlinks


def _resolve_use_color(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _color_wrap(s: str, use_color: bool) -> str:
    return f"{_ANSI_GREEN}{s}{_ANSI_RESET}" if use_color else s


@functools.lru_cache(maxsize=1)
def _diff_supports_color() -> bool:
    try:
        result = subprocess.run(
            ["diff", "--color=never", "--help"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return result.returncode == 0


def _diff_color_flag(color_mode: str) -> str | None:
    # --color is a GNU extension. Omit it entirely when color is disabled so
    # the normal/default diff path also works with BSD diff. An interactive
    # auto/always request uses color only when the installed diff supports it.
    if not _resolve_use_color(color_mode) or not _diff_supports_color():
        return None
    return "--color=always"


def _humanize_size_diff(diff_bytes: int) -> str:
    sign = "+" if diff_bytes >= 0 else "-"
    diff = abs(diff_bytes)
    if diff < 1024:
        return f"{sign}{diff} bytes"
    for unit, threshold in (
        ("TB", 1024**4),
        ("GB", 1024**3),
        ("MB", 1024**2),
        ("KB", 1024),
    ):
        if diff >= threshold:
            return f"{sign}{diff} bytes ({sign}{diff / threshold:.2f} {unit})"
    return f"{sign}{diff} bytes"


def _humanize_duration(diff_sec: int) -> str:
    sign = "+" if diff_sec >= 0 else "-"
    diff = abs(diff_sec)
    if diff == 0:
        return "+0s"
    days, rem = divmod(diff, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds:
        parts.append(f"{seconds}s")
    return sign + " ".join(parts[:2])


def _fmt_mtime(mtime_ns: int, *, subsecond: bool = False) -> str:
    secs, frac_ns = divmod(mtime_ns, 1_000_000_000)
    try:
        text = datetime.datetime.fromtimestamp(secs).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return f"{mtime_ns}ns"
    if subsecond:
        text += "." + (f"{frac_ns:09d}".rstrip("0") or "0")
    return text


def _mtime_mismatch(entry_mtime_ns: int, loc_mtime_ns: int, use_color: bool) -> tuple[str, str]:
    """Build the ("mtime", detail-line) pair for a manifest/local mtime
    mismatch, shared by the regular-file and symlink mtime checks so both
    render identically."""
    diff_ns = loc_mtime_ns - entry_mtime_ns
    # A sub-second drift (e.g. WSL2 drvfs truncates a restored mtime to whole
    # seconds) renders as two identical second-precision timestamps, so show
    # the fractional digits that actually differ.
    subsecond = abs(diff_ns) < 1_000_000_000
    fmt_local = _fmt_mtime(loc_mtime_ns, subsecond=subsecond)
    fmt_remote = _fmt_mtime(entry_mtime_ns, subsecond=subsecond)
    if entry_mtime_ns < loc_mtime_ns:
        cmp = "<"
        remote_disp = fmt_remote
        local_disp = _color_wrap(fmt_local, use_color)
    else:
        cmp = ">"
        remote_disp = _color_wrap(fmt_remote, use_color)
        local_disp = fmt_local
    if subsecond:
        diff_str = f"{diff_ns / 1_000_000_000:+.9f}".rstrip("0") + "s"
    else:
        # Difference of the displayed whole seconds, so the number always
        # agrees with the two rendered timestamps.
        diff_str = _humanize_duration(
            loc_mtime_ns // 1_000_000_000 - entry_mtime_ns // 1_000_000_000
        )
    return "mtime", f"mtime: remote={remote_disp} {cmp} local={local_disp} ({diff_str})"


def mode_differs(entry: ManifestEntry, st: os.stat_result) -> bool:
    """Whether the record's permission bits differ from the local stat's.

    The shared mode predicate: ``status``'s mode report and push's
    manifest-refresh check both use it, so a push settles exactly the mode
    differences ``status`` shows. Callers skip symlink records (their
    permission bits are compared nowhere)."""
    if stat_mod.S_IMODE(st.st_mode) == entry.perm_bits:
        return False
    if IS_WINDOWS:
        # Windows-native Python (incl. msys2 UCRT64) reports synthetic modes
        # via os.stat: 0o666 for writable files, 0o444 for read-only - not
        # the Unix permission bits. Only the owner-write bit is meaningful.
        return (entry.perm_bits & 0o200) != (st.st_mode & 0o200)
    return True


@dataclass
class EntryDiff:
    status: str | None  # None=match, "M"=modified, "D"=missing/wrong-type
    tags: list[str]  # ["mode", "mtime", "size", "link"]
    details: list[str]  # human-readable per-field detail lines

    @property
    def is_match(self) -> bool:
        return self.status is None


def compare_to_local(
    entry: ManifestEntry,
    target: str,
    *,
    window_ns: int,
    use_color: bool = False,
) -> EntryDiff:
    """Manifest record vs local filesystem state, stat'd here.

    The thin lstat-taking wrapper over :func:`compare_to_stat` for callers
    that hold only a path (the pull no-op gate, single-file status); the
    status merge-join already holds the walk's lstat and calls
    ``compare_to_stat`` directly.
    """
    try:
        st: os.stat_result | None = os.lstat(target)
    except OSError:
        st = None
    local_sym: str | None = None
    if st is not None and stat_mod.S_ISLNK(st.st_mode):
        try:
            local_sym = os.readlink(target)
        except OSError:
            local_sym = ""
    return compare_to_stat(
        entry,
        st,
        local_sym,
        window_ns=window_ns,
        use_color=use_color,
    )


def compare_to_stat(
    entry: ManifestEntry,
    st: os.stat_result | None,
    local_sym: str | None,
    *,
    window_ns: int,
    use_color: bool = False,
) -> EntryDiff:
    """Manifest record vs the local side's already-taken ``lstat``.

    ``st`` is the local lstat (None = missing) and ``local_sym`` the readlink
    target when ``st`` is a symlink - both come for free from the manifest
    walk, so the status merge-join adds no syscalls here.
    The size + mtime part is the same check the sync's ManifestFilter
    applies (mtime within ``window_ns``), so `status` and push/pull agree on
    what counts as changed; mode is additionally compared here for the
    metadata report (a mode change never re-transfers data - push refreshes
    the manifest instead, through the same ``mode_differs`` predicate). A
    directory's own mtime is compared the same as any other record's: push
    tracks its drift and refreshes the record, so status reports it and pull
    restores it, the same as any other tracked mtime.
    """
    diff = EntryDiff(status=None, tags=[], details=[])

    if entry.sym_target is not None:
        if st is None:
            diff.status = "D"
            return diff
        if not stat_mod.S_ISLNK(st.st_mode):
            _mark_type_change(diff, entry.mode, st.st_mode)
            return diff
        loc_link = local_sym if local_sym is not None else ""
        if loc_link != entry.sym_target:
            diff.status = "M"
            diff.tags.append("link")
            diff.details.append(f"link: remote={entry.sym_target} local={loc_link}")
        if (
            SYMLINK_MTIME_SUPPORTED
            and entry.mtime_ns is not None
            and abs(st.st_mtime_ns - entry.mtime_ns) > window_ns
        ):
            diff.status = "M"
            tag, detail = _mtime_mismatch(entry.mtime_ns, st.st_mtime_ns, use_color)
            diff.tags.append(tag)
            diff.details.append(detail)
        return diff

    if st is None:
        diff.status = "D"
        return diff

    if stat_mod.S_IFMT(entry.mode) != stat_mod.S_IFMT(st.st_mode):
        # A type change is a modification the next push acts on (it
        # re-records the new kind), so it prints as M with a `type` tag -
        # unlike a manifest-only record, which a plain push leaves alone.
        _mark_type_change(diff, entry.mode, st.st_mode)
        return diff

    is_dir_local = stat_mod.S_ISDIR(st.st_mode)

    if not is_dir_local:
        loc_size = st.st_size
        if entry.size is not None and loc_size != entry.size:
            diff.status = "M"
            diff.tags.append("size")
            if entry.size < loc_size:
                cmp = "<"
                remote_disp = str(entry.size)
                local_disp = _color_wrap(str(loc_size), use_color)
            else:
                cmp = ">"
                remote_disp = _color_wrap(str(entry.size), use_color)
                local_disp = str(loc_size)
            diff_str = _humanize_size_diff(loc_size - entry.size)
            diff.details.append(f"size: remote={remote_disp} {cmp} local={local_disp} ({diff_str})")

    if mode_differs(entry, st):
        loc_mode = format(stat_mod.S_IMODE(st.st_mode), "o")
        diff.status = "M"
        diff.tags.append("mode")
        diff.details.append(f"mode: remote={entry.perm_str} local={loc_mode}")

    if entry.mtime_ns is not None and abs(st.st_mtime_ns - entry.mtime_ns) > window_ns:
        diff.status = "M"
        tag, detail = _mtime_mismatch(entry.mtime_ns, st.st_mtime_ns, use_color)
        diff.tags.append(tag)
        diff.details.append(detail)

    return diff


def _kind_name(mode: int) -> str:
    if stat_mod.S_ISREG(mode):
        return "regular file"
    if stat_mod.S_ISDIR(mode):
        return "directory"
    if stat_mod.S_ISLNK(mode):
        return "symlink"
    return "special file"


def _mark_type_change(diff: EntryDiff, recorded_mode: int, local_mode: int) -> None:
    diff.status = "M"
    diff.tags.append("type")
    diff.details.append(f"type: remote={_kind_name(recorded_mode)} local={_kind_name(local_mode)}")


def format_diff_block(diff: EntryDiff, target: str, verbose: bool) -> str | None:
    if diff.is_match:
        return None
    if diff.status == "D":
        block = f"D {target}\n"
    else:
        block = f"M {target}\t{', '.join(diff.tags)}\n"
    if verbose:
        for d in diff.details:
            block += f"      {d}\n"
    return block
