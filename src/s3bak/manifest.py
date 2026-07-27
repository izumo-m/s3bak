"""Manifest v3: JSONL read/write, the sorted-stream merge-join, the push
journal, and the stat-based pull compare (ManifestFilter).

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
walk -> file, and the push-journal merge, the ``--meta-only`` rewrite, the
pull compare (ManifestFilter), and the status / pull ``--delete`` diff
(merge_join) are all merges of sorted streams instead of a read-all + sort.

The push journal (docs/journal.md) is the diff a push's single scan emits:
one line per manifest change, a one-character marker (``+`` add / ``!``
replace / ``-`` drop) followed by a manifest record line, in sort-key order.
``merge_journal`` applies it to the old manifest; the emitter
(syncops.PushJournal) holds every policy decision, so the merge is a pure
apply.
"""

from __future__ import annotations

import fnmatch
import json
import os
import stat as stat_mod
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, TypeVar

try:
    import grp
except ModuleNotFoundError:
    grp = None  # type: ignore[assignment]
try:
    import pwd
except ModuleNotFoundError:
    pwd = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from boto3_s3 import SyncPair

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


def _fsencodable(s: str) -> bool:
    """Whether ``s`` can round-trip to a filesystem path. A surrogateescape'd
    undecodable byte (``\\udc80``-``\\udcff``) round-trips (POSIX filenames use
    those), but a lone surrogate like ``\\ud800`` - which a damaged/hostile
    manifest can carry - raises UnicodeEncodeError deep in os.symlink/os.stat,
    uncaught by run(), and only after _place_symlink has already removed the
    existing file (data loss + traceback). Reject it at parse time instead."""
    try:
        os.fsencode(s)
        return True
    except (UnicodeEncodeError, ValueError):
        return False


# A recorded size beyond a signed 64-bit off_t is not a real file size; it is a
# damaged/hostile manifest. Rejecting it here keeps the human-readable size
# formatting (compare.py divides by a unit threshold) from an OverflowError on a
# value too large to convert to float.
_MAX_SIZE = (1 << 63) - 1


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
            # The path must round-trip to a filesystem path: a lone surrogate
            # (unlike a surrogateescape'd byte) is not fsencodable and would
            # crash os.path.join/os.stat on restore.
            or not _fsencodable(path)
            or mode < 0
            or mode > 0o177777
            or not (size is None or (isinstance(size, int) and not isinstance(size, bool)))
            # A negative or absurdly-large size is a damaged record; the upper
            # bound also keeps compare.py's float size formatting from overflowing.
            or (isinstance(size, int) and not 0 <= size <= _MAX_SIZE)
            or not (
                mtime_ns is None or (isinstance(mtime_ns, int) and not isinstance(mtime_ns, bool))
            )
            # A NUL, empty, or non-fsencodable link target survives every type
            # check but raises ValueError/UnicodeEncodeError/FileNotFoundError deep
            # in os.symlink/os.path.isdir on restore, which run() does not catch -
            # and only AFTER _place_symlink removed the existing file (data loss +
            # traceback). A symlink record with no target at all is equally
            # unrestorable. Reject both here so a damaged manifest fails closed.
            or not (
                link is None
                or (
                    isinstance(link, str)
                    and link != ""
                    and "\x00" not in link
                    and _fsencodable(link)
                )
            )
            or (stat_mod.S_ISLNK(mode) and link is None)
            # owner/group are display-only, but a lone surrogate here is not
            # UTF-8-encodable and crashes ls-remote's stdout write; reject it.
            or not isinstance(owner, str)
            or not _fsencodable(owner)
            or not isinstance(group, str)
            or not _fsencodable(group)
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
    except (json.JSONDecodeError, KeyError, ValueError, TypeError, RecursionError):
        # RecursionError: a deeply-nested JSON value in an unknown field.
        # ValueError also covers an integer literal too long to convert.
        return None


def _check_header(line: str) -> None:
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError, RecursionError):
        # A malformed header, an over-long integer literal, or a deeply-nested
        # value: a damaged manifest, not a Python traceback for the CLI user.
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
                    or (os.name == "nt" and "\\" in entry.path)  # a path separator on Windows
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
            # A POSIX filename may contain a backslash; on Windows it is a path
            # separator, so a record like "./a\\b" (a single file on POSIX) would
            # restore as a nested dir a / file b. Fail closed there before any
            # transfer; on POSIX os.name != "nt", so this is a no-op.
            if os.name == "nt" and any("\\" in part for part in parts):
                raise ManifestError(
                    f"manifest path component contains a backslash,"
                    f" unrestorable on Windows: {entry.path!r}"
                )
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


def iter_manifest_raw(manifest_path: str) -> Iterator[tuple[str, str, ManifestEntry, str]]:
    """Stream ``(sort_key, rel, entry, line)`` for every record, root included
    (path ``"."`` -> rel ``"."``, sort key ``""``). The raw line (no trailing
    newline) lets a merge copy an untouched record verbatim, preserving any
    unknown keys. Fails closed like ``iter_manifest``."""
    try:
        with open(manifest_path, encoding="utf-8") as f:
            _check_header(f.readline())
            for line_number, line in enumerate(f, start=2):
                e = parse_entry(line)
                if e is None:
                    raise ManifestError(f"invalid manifest record at line {line_number}")
                rel = "." if e.path == "." else e.path.removeprefix("./")
                yield entry_sort_key(e.path, e.is_dir), rel, e, line.rstrip("\n")
    except UnicodeError as e:
        raise ManifestError(f"manifest is not valid UTF-8: {e}") from e


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
    obj: dict[str, str | int] = {
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


class RecordedFiles:
    """Streaming membership test over a manifest's regular-file records - the
    records that own an S3 object. Queries must arrive in ascending key order
    (they do: the delete lane decides serially in S3 key order, and a file's
    sort key is its bare rel), so a one-record lookahead answers every query.
    ``path=None`` means no manifest is on S3: nothing is recorded."""

    def __init__(self, path: str | None) -> None:
        self._records: Iterator[ManifestEntry] | None = (
            iter_manifest(path) if path is not None else None
        )
        self._head: str | None = None
        self._advance()

    def _advance(self) -> None:
        if self._records is not None:
            for e in self._records:
                if e.is_file:
                    self._head = e.path.removeprefix("./")
                    return
        self._head = None

    def contains(self, rel: str) -> bool:
        while self._head is not None and self._head < rel:
            self._advance()
        return self._head == rel

    def close(self) -> None:
        """Release the manifest file handle the record stream holds open (an
        open file cannot be removed on Windows). Idempotent."""
        closer = getattr(self._records, "close", None)
        if callable(closer):
            closer()
        self._head = None


class _RestorabilityWarner:
    """Warn once per subtree that a merged manifest leaves unrestorable.

    Every emitted record passes through ``emit``, which tracks the most recent
    non-directory records whose descendant key range is still open
    ("blockers", a stack because siblings like ``sub.txt`` sort between a file
    ``sub`` and the ``sub/...`` range). A record landing under a blocker means
    records survive under a path another record says is not a directory (a
    local change replaced a directory with a file, or vice versa): they stay
    backed up but ``pull`` cannot materialize both."""

    def __init__(self, out: IO[str], warn: Callable[[str], None] | None) -> None:
        self._out = out
        self._warn = warn
        self._blockers: list[tuple[str, bool]] = []  # (rel, warned)

    def emit(self, rel: str, is_dir: bool, text: str) -> None:
        key = rel + "/" if is_dir else rel
        blockers = self._blockers
        while blockers:
            top_rel = blockers[-1][0]
            if rel == top_rel or rel.startswith(top_rel + "/"):
                if not blockers[-1][1] and self._warn is not None:
                    self._warn(
                        f"warning: manifest keeps records under non-directory"
                        f" ./{top_rel}; pull cannot restore them"
                        f" (push --delete prunes them)"
                    )
                    blockers[-1] = (top_rel, True)
                break
            if key > top_rel + "/":
                blockers.pop()
                continue
            break  # a sibling like `{top_rel}.x`: the blocker's range is still ahead
        if not is_dir and rel != ".":
            blockers.append((rel, False))
        self._out.write(text)


def write_merged(
    out: IO[str],
    old_manifest: str | None,
    sub: str | None,
    new_entries: Iterable[tuple[str, os.stat_result, str | None]],
    *,
    keep_old: bool = False,
    warn: Callable[[str], None] | None = None,
) -> None:
    """Write a v3 manifest merging a fresh local walk into the old manifest -
    the ``--meta-only`` rewrite (an ordinary push merges its journal instead,
    see ``merge_journal``).

    Old records outside the replaced range (everything when ``sub`` is None,
    the records at/under ``sub`` otherwise) are copied verbatim (preserving
    any unknown keys). Inside the range, a walked path always wins over its
    old record and old-only records survive when ``keep_old`` is True (the
    ``--meta-only`` keep merge; False drops them - the sub-path removal).
    Everything streams in sort-key order: one record of lookahead per input
    (merge_join).

    ``warn`` receives the ``_RestorabilityWarner`` message for records kept
    under a non-directory path.
    """
    out.write(_header_line() + "\n")
    warner = _RestorabilityWarner(out, warn)

    def old_items() -> Iterator[tuple[str, tuple[str, ManifestEntry, str]]]:
        if old_manifest is None:
            return
        for key, rel, e, line in iter_manifest_raw(old_manifest):
            yield key, (rel, e, line)

    def walk_items() -> Iterator[tuple[str, tuple[str, os.stat_result, str | None]]]:
        for item in new_entries:
            path, st, _sym = item
            yield entry_sort_key(path, stat_mod.S_ISDIR(st.st_mode)), item

    for _key, old, new in merge_join(old_items(), walk_items()):
        if new is not None:  # the fresh walk record wins over any old record
            path, st, sym_target = new
            rel = "." if path == "." else path.removeprefix("./")
            line = format_entry(path, st, sym_target) + "\n"
            warner.emit(rel, stat_mod.S_ISDIR(st.st_mode), line)
            continue
        assert old is not None
        rel, e, line = old
        in_range = sub is None or rel == sub or rel.startswith(sub + "/")
        if in_range and not keep_old:
            continue
        warner.emit(rel, e.is_dir, line + "\n")


# =============================================================================
# The push journal (docs/journal.md)
# =============================================================================

JOURNAL_ADD = "+"
JOURNAL_REPLACE = "!"
JOURNAL_DROP = "-"


def iter_journal(journal_path: str) -> Iterator[tuple[str, str, ManifestEntry, str]]:
    """Stream ``(sort_key, marker, entry, payload_line)`` from a push journal.

    Validates the journal's own shape fail closed: a known marker, a payload
    that parses as a manifest record, and strictly ascending sort keys (at
    most one event per key - a ``-`` plus ``+`` pair at one key is an emitter
    bug that must have been a ``!``). Marker consistency against the old
    manifest is ``merge_journal``'s job, where the old record is in hand."""
    previous: str | None = None
    try:
        with open(journal_path, encoding="utf-8") as f:
            for line_number, line in enumerate(f, start=1):
                marker, payload = line[:1], line[1:]
                if marker not in (JOURNAL_ADD, JOURNAL_REPLACE, JOURNAL_DROP):
                    raise ManifestError(f"invalid journal marker at line {line_number}")
                e = parse_entry(payload)
                if e is None:
                    raise ManifestError(f"invalid journal record at line {line_number}")
                key = entry_sort_key(e.path, e.is_dir)
                if previous is not None and key <= previous:
                    raise ManifestError(
                        f"journal events are duplicated or out of order at {e.path!r}"
                    )
                previous = key
                yield key, marker, e, payload.rstrip("\n")
    except UnicodeError as exc:
        raise ManifestError(f"journal is not valid UTF-8: {exc}") from exc


def merge_journal(
    out: IO[str],
    old_manifest: str | None,
    journal_path: str,
    *,
    warn: Callable[[str], None] | None = None,
) -> None:
    """Write a v3 manifest applying a push journal to the old manifest.

    The 2-way streaming merge of the journal design: a key with no event
    copies its old record verbatim (unknown keys preserved), ``+`` / ``!``
    copy the event payload byte-for-byte (marker stripped, no
    re-serialization), ``-`` skips the old record. The merge applies events
    and knows no policy - every keep/drop decision was the emitter's.

    Markers are cross-checked against the old manifest, fail closed: a ``+``
    whose key exists, a ``!`` / ``-`` whose key does not, or a ``-`` payload
    that differs from the record it drops is an emitter bug and raises
    ``ManifestError`` rather than publishing a manifest built on it. ``warn``
    receives the same restorability message as ``write_merged``.
    """
    out.write(_header_line() + "\n")
    warner = _RestorabilityWarner(out, warn)

    def old_items() -> Iterator[tuple[str, tuple[str, ManifestEntry, str]]]:
        if old_manifest is None:
            return
        for key, rel, e, line in iter_manifest_raw(old_manifest):
            yield key, (rel, e, line)

    def events() -> Iterator[tuple[str, tuple[str, ManifestEntry, str]]]:
        for key, marker, e, payload in iter_journal(journal_path):
            yield key, (marker, e, payload)

    for _key, old, event in merge_join(old_items(), events()):
        if event is None:
            assert old is not None
            rel, e, line = old
            warner.emit(rel, e.is_dir, line + "\n")
            continue
        marker, e, payload = event
        rel = "." if e.path == "." else e.path.removeprefix("./")
        if marker == JOURNAL_ADD:
            if old is not None:
                raise ManifestError(f"journal adds an already-recorded path: {e.path!r}")
            warner.emit(rel, e.is_dir, payload + "\n")
        elif marker == JOURNAL_REPLACE:
            if old is None:
                raise ManifestError(f"journal replaces an unrecorded path: {e.path!r}")
            warner.emit(rel, e.is_dir, payload + "\n")
        else:  # JOURNAL_DROP
            if old is None:
                raise ManifestError(f"journal drops an unrecorded path: {e.path!r}")
            if payload != old[2]:
                raise ManifestError(f"journal drop does not match the record at {e.path!r}")


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


# =============================================================================
# ManifestFilter (the pull compare)
# =============================================================================


class ManifestFilter:
    """Pull's update-lane strategy: an rsync-style size+mtime check against
    the manifest (True = copy). Wired as ``S3.sync``'s ``update_filter`` on
    ``sync_down``, so it is handed only the both-sides download pairs; new
    entries (``create_filter``) and local extras are decided by those lanes,
    never here. (Push's compare lives in ``syncops.PushJournal``, which folds
    the same size+mtime judgment into its journal emission.)

    Streaming: it reads the manifest once, front to back, merge-joining its
    records against ``S3.sync``'s ascending compare-key pairs - the whole
    manifest is never held in memory. This works because the manifest is
    written in ``LocalStorage.scan`` order (``entry_sort_key``) and the
    update lane is decided serially on one thread in that same order, so a
    one-record lookahead suffices - the cursor self-heals over any key it is
    not asked about. A filter is a forward-only cursor: one serves exactly
    one sync.

    A pair is skipped only when the local side's size and mtime both match the
    manifest record (mtime within ``window_ns``) and the remote side has the
    recorded size too. Everything else copies: a key the manifest does not know,
    or a drifted stat. Pure stat work - no file content is read, and nothing
    beyond the sync's own listing is requested.

    Accepted blind spot: a change that leaves size+mtime equal to the record
    (a content edit with a restored mtime, or an S3-side write that bypassed
    s3bak at the same size) is invisible here; ``--checksum`` covers those.

    A STALE manifest (``push --data-only``, or out-of-band S3 writes) makes
    affected pairs re-transfer on every pull until a real push refreshes the
    record - pull never rewrites the manifest. Deliberate: the manifest is
    the record of the last real push, and only a push may change it.
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

    def __call__(self, pair: SyncPair) -> bool:
        # Only ever handed a both-sides download pair (pull's sync_down).
        if pair.transfer_type.value != "download":
            raise ValueError(f"ManifestFilter judges download pairs only: {pair.compare_key!r}")
        local, remote = pair.dest, pair.src

        m = self._lookup(pair.compare_key)
        if m is None or not m.is_file:
            # Both sides present, but the manifest is silent or records a
            # dir/symlink at this key: re-download an unknown key, but leave a
            # recorded non-file to apply_manifest, which recreates it with no
            # data object.
            return m is None
        if remote.size != m.size:
            return True  # the remote drifted from the record; size is free evidence
        try:
            # FileInfo.key is the absolute local path, '/'-separated.
            st = os.lstat(local.key.replace("/", os.sep))
        except OSError:
            return True  # vanished between listing and compare; let sync warn
        return not m.matches_stat(st, self.window_ns)
