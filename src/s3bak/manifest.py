"""Manifest v3: JSONL read/write, the sorted-stream merge-join, and the
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
(localwalk.py, boto3-s3's engine) emits in S3 key order, the writer streams
walk -> file, and the sub-path patch, the sync compare (ManifestFilter), and
the status / pull ``--delete`` diff (merge_join) are all merges of sorted
streams instead of a read-all + sort. This module is pure stdlib - the format,
the joins, and the pattern matching, with no boto3-s3 dependency.
"""

from __future__ import annotations

import fnmatch
import json
import os
import stat as stat_mod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import IO, Any, TypeVar

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
    """A manifest is corrupt or does not satisfy the current format."""


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
    """Parse one record, returning ``None`` for invalid input.

    ``iter_manifest`` turns ``None`` into a fail-closed ``ManifestError`` with
    the line number. Keeping the primitive non-raising is useful to the
    streaming patcher and focused format tests. Unknown keys are ignored.
    """
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
        owner = obj.get("owner", "")
        group = obj.get("group", "")
        if (
            not isinstance(path, str)
            or not path
            or "\x00" in path
            or mode < 0
            or mode > 0o177777
            or not (size is None or (isinstance(size, int) and not isinstance(size, bool)))
            or (isinstance(size, int) and size < 0)
            or not (
                mtime_ns is None or (isinstance(mtime_ns, int) and not isinstance(mtime_ns, bool))
            )
            or not (link is None or isinstance(link, str))
            or not isinstance(owner, str)
            or not isinstance(group, str)
        ):
            return None
        return ManifestEntry(
            path=path,
            mode=mode,
            owner=owner,
            group=group,
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
    """Stream a manifest's entries, failing closed on any damaged line.

    Treating a corrupt record as absent is unsafe: ``pull --delete`` could then
    classify the corresponding local path as an extra and remove it. A bad
    header, invalid UTF-8, or malformed entry therefore aborts the operation
    with ``ManifestError`` before any manifest-driven mutation starts.
    """
    try:
        with open(manifest_path, encoding="utf-8") as f:
            _check_header(f.readline())
            for line_number, line in enumerate(f, start=2):
                entry = parse_entry(line)
                if entry is None:
                    raise ManifestError(f"invalid manifest record at line {line_number}")
                yield entry
    except UnicodeError as e:
        raise ManifestError(f"manifest is not valid UTF-8: {e}") from e


def validate_manifest(manifest_path: str) -> str:
    """Validate whole-manifest invariants and return ``"dir"`` or ``"file"``.

    Writers always emit either a ``.``-rooted, strictly key-ordered directory
    tree or exactly one bare-basename regular-file record. Verifying that shape
    after download prevents corrupt/hostile records from confusing streaming
    joins or mapping several records onto one single-file restore target.
    """
    kind: str | None = None
    previous_key: str | None = None
    count = 0
    # Directory records must precede all of their descendants in key order.
    # Keeping only the current ancestry proves every record has a recorded
    # directory parent without turning validation into an O(tree-size) map.
    directory_stack: list[tuple[str, ...]] = [()]

    for entry in iter_manifest(manifest_path):
        count += 1
        if kind is None:
            if entry.path == ".":
                if not entry.is_dir or entry.sym_target is not None:
                    raise ManifestError("directory manifest root must be a directory record")
                kind = "dir"
            else:
                if (
                    entry.path.startswith("./")
                    or "/" in entry.path
                    or entry.path in (".", "..")
                    or not entry.is_file
                    or entry.sym_target is not None
                ):
                    raise ManifestError("single-file manifest must contain one regular-file record")
                kind = "file"
        elif kind == "file":
            raise ManifestError("single-file manifest contains more than one record")

        if kind == "dir" and entry.path != ".":
            if not entry.path.startswith("./"):
                raise ManifestError(f"invalid directory-manifest path: {entry.path!r}")
            parts = entry.path[2:].split("/")
            if any(part in ("", ".", "..") for part in parts):
                raise ManifestError(f"manifest path escapes restore root: {entry.path!r}")
            parent = tuple(parts[:-1])
            while directory_stack and len(directory_stack[-1]) > len(parent):
                directory_stack.pop()
            if not directory_stack or directory_stack[-1] != parent:
                raise ManifestError(f"manifest record has no directory parent: {entry.path!r}")
            if entry.is_dir:
                directory_stack.append(tuple(parts))

        is_symlink = stat_mod.S_ISLNK(entry.mode)
        if is_symlink != (entry.sym_target is not None):
            raise ManifestError(f"manifest type/link mismatch for {entry.path!r}")
        if entry.is_file:
            if entry.size is None:
                raise ManifestError(f"regular-file record has no size: {entry.path!r}")
        elif entry.size is not None:
            raise ManifestError(f"non-file record has a size: {entry.path!r}")
        if entry.mtime_ns is None:
            raise ManifestError(f"manifest record has no mtime_ns: {entry.path!r}")

        key = entry_sort_key(entry.path, entry.is_dir)
        if previous_key is not None and key <= previous_key:
            raise ManifestError(
                f"manifest records are duplicated or out of order at {entry.path!r}"
            )
        previous_key = key

    if count == 0:
        raise ManifestError("manifest contains no records")
    assert kind is not None
    return kind


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
    # ASCII escaping is not cosmetic: POSIX exposes undecodable filename bytes
    # as surrogate code points. Escaping them keeps the JSONL itself valid
    # UTF-8 and lets Python's filesystem surrogateescape round-trip the name.
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"))


def write_manifest(out: IO[str], entries: Iterable[tuple[str, os.stat_result, str | None]]) -> None:
    """Stream ``(path, lstat, sym_target)`` items to ``out`` as a v3 manifest.
    The items must already be in walk order (localwalk.walk_tree / iter_subtree)."""
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
                    raise ManifestError("invalid record in manifest being patched")
                rel = e.path.removeprefix("./")
                if rel == sub or rel.startswith(sub + "/"):
                    continue  # the replaced range
                flush_new_below(entry_sort_key(e.path, e.is_dir))
                out.write(line if line.endswith("\n") else line + "\n")
    flush_new_below(None)


# =============================================================================
# Exclude pattern matching (find -path semantics: * matches /)
# =============================================================================


def path_match(path: str, pattern: str) -> bool:
    # fnmatchcase is platform-neutral and, because it matches plain strings
    # rather than filesystem components, ``*`` also matches ``/``. Its mature
    # translator handles malformed/range-heavy bracket expressions without the
    # regex compilation failures a hand-rolled translator can introduce.
    return fnmatch.fnmatchcase(path, pattern)


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
# Sorted-stream merge-join
# =============================================================================

_A = TypeVar("_A")
_B = TypeVar("_B")


def merge_join(
    a: Iterable[tuple[str, _A]], b: Iterable[tuple[str, _B]]
) -> Iterator[tuple[str, _A | None, _B | None]]:
    """Join two ascending ``(sort_key, item)`` streams on their keys.

    Yields ``(key, a_item | None, b_item | None)`` for every key present on
    either side, in key order; a one-sided key carries ``None`` for the other
    side. One-record lookahead per stream, so joining a manifest against a
    fresh walk (both in ``entry_sort_key`` order) never holds more than two
    records in memory - the status / pull ``--delete`` diff works on manifests
    of any size. Both inputs must ascend strictly (no duplicate keys within a
    stream), which the walk and the manifest do by construction."""
    ita, itb = iter(a), iter(b)
    ha = next(ita, None)
    hb = next(itb, None)
    while ha is not None or hb is not None:
        if hb is None or (ha is not None and ha[0] < hb[0]):
            assert ha is not None
            yield ha[0], ha[1], None
            ha = next(ita, None)
        elif ha is None or hb[0] < ha[0]:
            yield hb[0], None, hb[1]
            hb = next(itb, None)
        else:
            yield ha[0], ha[1], hb[1]
            ha = next(ita, None)
            hb = next(itb, None)


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
    same semantics the manifest walk applies - re-rooting a sub-path
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
    """The default update-lane strategy for sync: an rsync-style size+mtime
    check against the manifest (True = copy). Wired as ``S3.sync``'s
    ``update_filter``, so it is handed only the both-sides pairs; new entries
    (``create_filter``) and orphans (``delete_filter``) are decided by those
    lanes, never here.

    Streaming: it reads the manifest once, front to back, merge-joining its
    records against ``S3.sync``'s ascending compare-key pairs - the whole
    manifest is never held in memory. This works because the manifest is
    written in ``LocalStorage.scan`` order (``entry_sort_key``) and a bare
    ``update_filter`` is decided serially on one thread in that same order
    (``--checksum``'s ``ParallelFilter`` content strategy is the only concurrent
    path, and it never wraps this filter), so a one-record lookahead suffices -
    the cursor self-heals over any key it is not asked about. A filter is a
    forward-only cursor: one serves exactly one sync.

    A pair is skipped only when the local side's size and mtime both match the
    manifest record (mtime within ``window_ns``) and the remote side has the
    recorded size too. Everything else copies: a key the manifest does not know,
    or a drifted stat. Pure stat work - no file content is read, and nothing
    beyond the sync's own listing is requested.

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
        # As an ``update_filter`` this is only ever handed a both-sides pair
        # (source and destination both present); the create lane (source-only)
        # and delete lane (destination-only) never reach here.
        direction = pair.transfer_type.value
        if direction == "upload":
            local, remote = pair.src, pair.dest
        elif direction == "download":
            local, remote = pair.dest, pair.src
        else:
            raise ValueError(f"ManifestFilter cannot judge a {direction!r} pair: {pair.key!r}")

        m = self._lookup(pair.key)
        if m is None or not m.is_file:
            # Both sides present, but the manifest is silent or records a
            # dir/symlink at this key: a push re-uploads it (local is the source
            # of truth); a pull re-downloads an unknown key but leaves a recorded
            # non-file to apply_manifest, which recreates it with no data object.
            return m is None if direction == "download" else True
        if remote.size != m.size:
            return True  # the remote drifted from the record; size is free evidence
        try:
            # FileInfo.key is the absolute local path, '/'-separated.
            st = os.lstat(local.key.replace("/", os.sep))
        except OSError:
            return True  # vanished between listing and compare; let sync warn
        return not m.matches_stat(st, self.window_ns)
