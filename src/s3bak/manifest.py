"""Manifest v3: JSONL read/write, the S3-key-ordered tree walk, and the
stat-based sync compare (ManifestFilter).

A manifest is stored as ``<entry>-manifest.jsonl`` next to the entry's data
(the suffix cannot collide with a single-file entry's own key, unlike a bare
``<entry>.json`` would). Line 1 is the header ``{"s3bak_manifest": 3}``; every
following line is one JSON object per tree entry, in S3 key order (aws-cli
byte order, where a directory's contents sort at ``name/...`` - so ``foo.txt``
comes before ``foo/bar``). Entry keys:

    path      "." (entry root), "./sub/path" below it, or a bare basename
              (single-file entry) - the entry-relative path, same convention
              the transfer keys use
    mode      full st_mode as an octal string ("100644", "40755", "120777")
    owner     user name, or the uid as a string when unresolvable
    group     group name, or the gid as a string
    size      byte size (regular files only)
    mtime_ns  st_mtime_ns
    link      symlink target (symlinks only)

Readers ignore unknown keys, so future fields never need a format bump; the
header version only changes when an existing key's meaning does.

The sorted-order invariant is what keeps everything here streaming: the walk
emits in S3 key order with one directory level of memory, the writer streams
walk -> file, and the sub-path patch is a merge of two sorted streams instead
of a read-all + sort.
"""

from __future__ import annotations

import json
import os
import re
import stat as stat_mod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import IO, Any

try:
    import grp
except ModuleNotFoundError:
    grp = None  # type: ignore[assignment]
try:
    import pwd
except ModuleNotFoundError:
    pwd = None  # type: ignore[assignment]

MANIFEST_SUFFIX = "-manifest.jsonl"
FORMAT_VERSION = 3
_HEADER_KEY = "s3bak_manifest"


def manifest_key(entry: str) -> str:
    """S3 key of an entry's manifest, relative to the configured prefix."""
    return f"{entry}{MANIFEST_SUFFIX}"


class ManifestError(Exception):
    """A manifest file is not readable as the current format (corrupt header
    or a version this build does not understand)."""


# =============================================================================
# Entries
# =============================================================================


@dataclass(frozen=True)
class ManifestEntry:
    path: str  # ".", "./foo/bar", or a bare basename (single-file entry)
    mode: int  # full st_mode (type + permission bits)
    owner: str
    group: str
    size: int | None  # regular files only
    mtime_ns: int | None
    sym_target: str | None

    @property
    def is_dir(self) -> bool:
        return stat_mod.S_ISDIR(self.mode)

    @property
    def is_file(self) -> bool:
        return stat_mod.S_ISREG(self.mode)

    @property
    def perm_bits(self) -> int:
        return stat_mod.S_IMODE(self.mode)

    @property
    def perm_str(self) -> str:
        return format(self.perm_bits, "o")

    def matches_stat(self, st: os.stat_result, window_ns: int) -> bool:
        """rsync-style size+mtime check for a regular file: same size, and mtime
        within ``window_ns``. A record without size/mtime never matches, so an
        indeterminate comparison falls on the transfer side."""
        if self.size is None or self.mtime_ns is None:
            return False
        if st.st_size != self.size:
            return False
        return abs(st.st_mtime_ns - self.mtime_ns) <= window_ns


def parse_entry(line: str) -> ManifestEntry | None:
    """One manifest line -> entry, or None for a blank/damaged line (never
    raises, so one bad line degrades to 'missing' instead of crashing status).
    Unknown keys are ignored (forward compatibility)."""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
        path = obj["path"]
        mode = int(obj["mode"], 8)
        size = obj.get("size")
        mtime_ns = obj.get("mtime_ns")
        link = obj.get("link")
        if (
            not isinstance(path, str)
            or not (size is None or isinstance(size, int))
            or not (mtime_ns is None or isinstance(mtime_ns, int))
            or not (link is None or isinstance(link, str))
        ):
            return None
        return ManifestEntry(
            path=path,
            mode=mode,
            owner=str(obj.get("owner", "")),
            group=str(obj.get("group", "")),
            size=size,
            mtime_ns=mtime_ns,
            sym_target=link,
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def _check_header(line: str) -> None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        obj = None
    if not isinstance(obj, dict) or _HEADER_KEY not in obj:
        raise ManifestError("not an s3bak v3 manifest (bad or missing header line)")
    if obj[_HEADER_KEY] != FORMAT_VERSION:
        raise ManifestError(
            f"unsupported manifest version {obj[_HEADER_KEY]!r} "
            f"(this s3bak reads version {FORMAT_VERSION})"
        )


def _header_line() -> str:
    return json.dumps({_HEADER_KEY: FORMAT_VERSION}, separators=(",", ":"))


def iter_manifest(manifest_path: str) -> Iterator[ManifestEntry]:
    """Stream a manifest's entries. Raises ManifestError on a bad header;
    damaged entry lines are skipped."""
    with open(manifest_path, encoding="utf-8") as f:
        _check_header(f.readline())
        for line in f:
            entry = parse_entry(line)
            if entry is not None:
                yield entry


def iter_compare_records(
    manifest_path: str, sub: str | None = None
) -> Iterator[tuple[str, ManifestEntry]]:
    """Stream ``(compare_sort_key, entry)`` for every non-root record in the
    sync's ascending compare-key order, optionally restricted to ``sub``.

    The key carries a directory's trailing ``/`` (``entry_sort_key`` semantics),
    so it merge-joins in lockstep with ``S3.sync``'s ascending pair keys - which
    lets ``ManifestFilter`` decide each pair with a one-record lookahead instead
    of loading the whole manifest. The tree root never forms a sync pair and is
    omitted; a record outside ``sub`` is skipped, and one under it has the
    ``sub/`` prefix stripped (matching the sync's sub-rooted compare keys)."""
    for e in iter_manifest(manifest_path):
        if e.path == ".":
            continue
        rel = e.path.removeprefix("./")
        if sub is not None:
            if not rel.startswith(sub + "/"):
                continue
            rel = rel[len(sub) + 1 :]
        yield (rel + "/" if e.is_dir else rel), e


# =============================================================================
# Writing
# =============================================================================


def _owner_group(st: os.stat_result) -> tuple[str, str]:
    if pwd is not None:
        try:
            owner = pwd.getpwuid(st.st_uid).pw_name
        except KeyError:
            owner = str(st.st_uid)
    else:
        owner = str(st.st_uid)
    if grp is not None:
        try:
            group = grp.getgrgid(st.st_gid).gr_name
        except KeyError:
            group = str(st.st_gid)
    else:
        group = str(st.st_gid)
    return owner, group


def format_entry(path: str, st: os.stat_result, sym_target: str | None) -> str:
    """One walk item -> one manifest line (no trailing newline)."""
    owner, group = _owner_group(st)
    obj: dict[str, Any] = {
        "path": path,
        "mode": format(st.st_mode, "o"),
        "owner": owner,
        "group": group,
    }
    if stat_mod.S_ISREG(st.st_mode):
        obj["size"] = st.st_size
    obj["mtime_ns"] = st.st_mtime_ns
    if sym_target is not None:
        obj["link"] = sym_target
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def write_manifest(out: IO[str], entries: Iterable[tuple[str, os.stat_result, str | None]]) -> None:
    """Stream ``(path, lstat, sym_target)`` items to ``out`` as a v3 manifest.
    The items must already be in walk order (walk_tree / iter_subtree)."""
    out.write(_header_line() + "\n")
    for path, st, sym_target in entries:
        out.write(format_entry(path, st, sym_target) + "\n")


def entry_sort_key(path: str, is_dir: bool) -> str:
    """The manifest ordering key: the tree root is "" (always first), a
    directory sorts at ``path/`` (right before its children, after ``path.x``
    siblings), everything else at its bare relative path - S3 key byte order."""
    if path == ".":
        return ""
    norm = path.removeprefix("./")
    return norm + "/" if is_dir else norm


def write_patched(
    out: IO[str],
    old_manifest: str | None,
    sub: str,
    new_entries: Iterable[tuple[str, os.stat_result, str | None]],
) -> None:
    """Rewrite a manifest with the records under ``sub`` replaced.

    Both inputs are in sort-key order, so this is a streaming merge: old lines
    outside ``sub`` are copied verbatim (preserving any unknown keys), old
    lines at/under ``sub`` are dropped, and the freshly walked ``new_entries``
    are spliced in at their sorted position. ``new_entries`` may be empty (the
    sub-path was deleted locally). ``old_manifest`` is the path of the previous
    manifest file, or None when none exists yet."""
    out.write(_header_line() + "\n")
    new_iter = iter(new_entries)
    pending = next(new_iter, None)

    def flush_new_below(limit: str | None) -> None:
        nonlocal pending
        while pending is not None:
            path, st, sym_target = pending
            key = entry_sort_key(path, stat_mod.S_ISDIR(st.st_mode))
            if limit is not None and key >= limit:
                return
            out.write(format_entry(path, st, sym_target) + "\n")
            pending = next(new_iter, None)

    if old_manifest is not None:
        with open(old_manifest, encoding="utf-8") as f:
            _check_header(f.readline())
            for line in f:
                e = parse_entry(line)
                if e is None:
                    continue  # drop a damaged line rather than re-emit it
                rel = e.path.removeprefix("./")
                if rel == sub or rel.startswith(sub + "/"):
                    continue  # the replaced range
                flush_new_below(entry_sort_key(e.path, e.is_dir))
                out.write(line if line.endswith("\n") else line + "\n")
    flush_new_below(None)


# =============================================================================
# Exclude pattern matching (find -path semantics: * matches /)
# =============================================================================

_pattern_cache: dict[str, re.Pattern[str]] = {}


def _glob_to_regex(pattern: str) -> str:
    result: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            result.append(".*")
        elif c == "?":
            result.append(".")
        elif c == "[":
            j = i + 1
            if j < len(pattern) and pattern[j] in "!^":
                j += 1
            if j < len(pattern) and pattern[j] == "]":
                j += 1
            while j < len(pattern) and pattern[j] != "]":
                j += 1
            result.append(pattern[i : j + 1])
            i = j
        else:
            result.append(re.escape(c))
        i += 1
    return "".join(result)


def path_match(path: str, pattern: str) -> bool:
    if pattern not in _pattern_cache:
        _pattern_cache[pattern] = re.compile(_glob_to_regex(pattern))
    return _pattern_cache[pattern].fullmatch(path) is not None


def split_excludes(excludes: list[str]) -> tuple[list[str], list[str]]:
    prune_patterns: list[str] = []
    skip_patterns: list[str] = []
    for ex in excludes:
        if ex.endswith("/*"):
            prune_patterns.append(f"./{ex[:-2]}")
        else:
            skip_patterns.append(f"./{ex}")
    return prune_patterns, skip_patterns


# =============================================================================
# Tree walk (S3 key order)
# =============================================================================


def walk_tree(
    root: str, excludes: list[str], *, root_rel: str = ".", rel_prefix: str = "./"
) -> Iterator[tuple[str, os.stat_result, str | None]]:
    """Walk a directory tree yielding ``(rel, lstat, sym_target | None)`` in
    S3 key order. Each ``rel`` is what a record's ``path`` field stores.

    rel uses the manifest convention: ``root_rel`` for the root, ``rel_prefix``
    + path below it - for an entry push that is "." / "./sub/file"; a sub-path
    push passes "./{sub}" / "./{sub}/" so every rel (and thus every exclude
    match) stays anchored at the ENTRY root, where the configured patterns are
    defined. Each directory level is sorted with real directories keyed as
    ``name+"/"`` (files and symlinks as ``name``) and traversed depth-first
    inline, which makes the concatenated stream globally byte-ordered - the
    same order as boto3-s3's ``LocalStorage.walk_local`` and an S3 listing, so
    a manifest can be merge-joined against either. The traversal is iterative
    (an explicit stack), so tree depth is not bounded by the recursion limit;
    memory is one directory level per depth. An unreadable directory is
    silently skipped (as os.walk did) - the data sync's own scan already
    surfaces it as a warning. Symlinks are leaves (never followed).
    """
    prune_patterns, skip_patterns = split_excludes(excludes)
    yield root_rel, os.lstat(root), None

    stack: list[tuple[str, str, Iterator[tuple[str, str, bool]]]] = [
        (root, rel_prefix, iter(_scan_sorted(root)))
    ]
    while stack:
        dirpath, prefix, entries = stack[-1]
        item = next(entries, None)
        if item is None:
            stack.pop()
            continue
        _sort_name, name, is_real_dir = item
        rel = prefix + name
        full = os.path.join(dirpath, name)
        if is_real_dir:
            if any(path_match(rel, p) for p in prune_patterns):
                continue
            yield rel, os.lstat(full), None
            stack.append((full, rel + "/", iter(_scan_sorted(full))))
        else:
            if any(path_match(rel, p) for p in skip_patterns):
                continue
            st = os.lstat(full)
            if stat_mod.S_ISLNK(st.st_mode):
                # A symlink named like a pruned directory is excluded too (it
                # occupies the name the pattern targets).
                if any(path_match(rel, p) for p in prune_patterns):
                    continue
                yield rel, st, os.readlink(full)
            else:
                yield rel, st, None


def _scan_sorted(dirpath: str) -> list[tuple[str, str, bool]]:
    """One directory level as ``(sort_name, name, is_real_dir)`` in S3 key
    order (dirs keyed ``name+"/"``). Unreadable -> empty (silently skipped)."""
    entries: list[tuple[str, str, bool]] = []
    try:
        with os.scandir(dirpath) as it:
            for de in it:
                is_real_dir = de.is_dir(follow_symlinks=False)
                sort_name = de.name + "/" if is_real_dir else de.name
                entries.append((sort_name, de.name, is_real_dir))
    except OSError:
        return []
    entries.sort()
    return entries


def iter_subtree(
    local_sub: str, sub: str, excludes: list[str]
) -> Iterator[tuple[str, os.stat_result, str | None]]:
    """Walk items for a sub-path push: ``local_sub`` as recorded under
    ``./{sub}``. Handles the file / symlink / directory cases. The walk rels
    are entry-rooted, so the entry's exclude patterns apply exactly as they
    would in a full push."""
    st = os.lstat(local_sub)
    if stat_mod.S_ISLNK(st.st_mode):
        yield f"./{sub}", st, os.readlink(local_sub)
        return
    if not os.path.isdir(local_sub):
        yield f"./{sub}", st, None
        return
    yield from walk_tree(local_sub, excludes, root_rel=f"./{sub}", rel_prefix=f"./{sub}/")


def _excluded(rel: str, prune: list[str], skip: list[str]) -> bool:
    """Whether an entry-rooted ``./...`` rel is excluded: a skip pattern
    matches it, or a prune pattern matches it or any of its ancestors (a
    pruned directory excludes its whole subtree)."""
    if any(path_match(rel, p) for p in skip):
        return True
    anc = rel
    while True:
        if any(path_match(anc, p) for p in prune):
            return True
        i = anc.rfind("/")
        if i <= 1:  # reached "./"
            return False
        anc = anc[:i]


def exclude_filter(excludes: list[str], sub: str | None = None) -> Any:
    """The sync ``filter=`` for an entry's excludes (True = keep).

    Matches each side's compare_key against the entry-rooted patterns - the
    same semantics walk_tree applies to the manifest - re-rooting a sub-path
    sync's keys with ``./{sub}/`` so the entry's patterns keep their meaning.
    Applies to both sides of the sync, so an excluded key is neither
    transferred nor treated as a delete candidate."""
    prune, skip = split_excludes(excludes)
    prefix = f"./{sub}/" if sub else "./"

    def keep(info: Any) -> bool:
        return not _excluded(prefix + (info.compare_key or ""), prune, skip)

    return keep


# =============================================================================
# ManifestFilter (the default sync compare)
# =============================================================================


class ManifestFilter:
    """The default ``compare=`` strategy for sync: an rsync-style size+mtime
    check against the manifest (True = copy).

    Streaming: it reads the manifest once, front to back, merge-joining its
    records against ``S3.sync``'s ascending compare-key pairs - the whole
    manifest is never held in memory. This works because the manifest is
    written in ``LocalStorage.scan`` order (``entry_sort_key``) and a bare
    ``PairFilter`` is decided serially on one thread in that same order
    (``ParallelCompare`` - opt-in, content strategies only - is the only
    concurrent path), so a one-record lookahead suffices. A filter is a
    forward-only cursor: one serves exactly one sync.

    A pair is skipped only when the local side's size and mtime both match the
    manifest record (mtime within ``window_ns``) and the remote side has the
    recorded size too. Everything else copies: a missing side, a key the
    manifest does not know, a drifted stat. Pure stat work - no file content
    is read, and nothing beyond the sync's own listing is requested.

    Accepted blind spot: a change that leaves size+mtime equal to the record
    (a content edit with a restored mtime, or an S3-side write that bypassed
    s3bak at the same size) is invisible here; ``--checksum`` covers those.

    A spurious mtime-only difference self-heals on push: the file is
    re-transferred once, the manifest is refreshed with the new mtime, and
    later runs pass the size+mtime check again. The converse does not hold for a
    STALE manifest (``push --data-only``, or out-of-band S3 writes): pull
    never rewrites the manifest, so affected pairs re-transfer on every pull
    until a full push refreshes the record. Deliberate: the manifest is the
    record of the last real push, and only a push may change it.
    """

    def __init__(self, records: Iterator[tuple[str, ManifestEntry]], *, window_ns: int):
        # (compare_sort_key, entry) in ascending key order - see iter_compare_records.
        self._records = iter(records)
        self.window_ns = window_ns
        self._head: tuple[str, ManifestEntry] | None = next(self._records, None)

    def close(self) -> None:
        """Release the manifest file handle the record stream holds open. The
        sync keeps this filter alive for its whole run, so the caller closes it
        before unlinking the temp manifest (an open file cannot be removed on
        Windows). Idempotent, and a no-op for a non-generator record source."""
        closer = getattr(self._records, "close", None)
        if callable(closer):
            closer()
        self._head = None

    def _lookup(self, key: str) -> ManifestEntry | None:
        """The record for compare key ``key``, or None. Advances the one-record
        cursor: skip records ordered before ``key`` (keys the sync did not pair,
        e.g. deleted files), consume an exact hit, stop past a larger one.
        Correct only because both sides ascend in the same compare-key order."""
        while self._head is not None and self._head[0] < key:
            self._head = next(self._records, None)
        if self._head is not None and self._head[0] == key:
            entry = self._head[1]
            self._head = next(self._records, None)
            return entry
        return None

    def __call__(self, pair: Any) -> bool:
        if pair.src is None:
            return False  # destination-only: a delete candidate, never a copy
        direction = pair.transfer_type.value
        if direction == "upload":
            local, remote = pair.src, pair.dest
        elif direction == "download":
            local, remote = pair.dest, pair.src
        else:
            raise ValueError(f"ManifestFilter cannot judge a {direction!r} pair: {pair.key!r}")

        m = self._lookup(pair.key)
        if m is None or not m.is_file:
            # Unknown to the manifest, or recorded as a dir/symlink: a push
            # uploads it (local is the source of truth); a pull downloads an
            # unknown key but skips a non-file - apply_manifest recreates
            # those from the manifest, no data object needed.
            return m is None if direction == "download" else True
        if local is None or remote is None:
            return True  # exists on one side only
        if remote.size != m.size:
            return True  # the remote drifted from the record; size is free evidence
        try:
            # FileInfo.key is the absolute local path, '/'-separated.
            st = os.lstat(local.key.replace("/", os.sep))
        except OSError:
            return True  # vanished between listing and compare; let sync warn
        return not m.matches_stat(st, self.window_ns)
