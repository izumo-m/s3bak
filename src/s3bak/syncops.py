# Requires Python 3.10+
"""The manifest <-> S3 bridge and download orchestration.

Writes v3 manifests to S3 (the journal merge, and the single-file entry's
one-record write), downloads a manifest or a data tree, and owns the push's
journal emitter (``PushJournal``) and pull's compare strategy. This is the
seam between the pure manifest format (manifest.py), the S3 backend
(store.py), and the command layer (commands.py).
"""

from __future__ import annotations

import json
import os
import stat as stat_mod
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING

from boto3_s3 import LocalFileInfo, SrcOnlyPair, SyncPair

from s3bak import localwalk, manifest
from s3bak.compare import (
    CONFLICT_CONTENT,
    CONFLICT_OBJECT,
    CONFLICT_SIZE,
    CONFLICT_TYPE,
    CONFLICT_UNRECORDED,
    SYMLINK_MTIME_SUPPORTED,
    mode_differs,
    ordered_side,
    tie_conflict,
    warn_conflict,
)
from s3bak.config import Config, Opts
from s3bak.console import console
from s3bak.manifest import ManifestEntry

if TYPE_CHECKING:
    from boto3_s3 import FileFilter, FileInfo, MergedPair, PairFilter


def _walk_warning(body: str) -> None:
    """The manifest walk's warn hook: boto3-s3 message bodies -> one warning
    line each (exit 2), aws-cli's own prefix included."""
    console.warn(f"warning: {body}")


def write_manifest_to_aws(
    cfg: Config,
    entry: str,
    target: str,
    verbose: bool,
    *,
    upload: bool = True,
) -> None:
    """Write a single-file entry's one-record v3 manifest from a fresh lstat
    to a temp file and upload it (an ordinary directory push merges its
    journal instead, see ``publish_journal_manifest``). ``upload=False`` is
    the dry-run preview: the record write and validation run for real and
    only the S3 write is skipped."""
    key = manifest.manifest_key(entry)
    if upload:
        console.diag(f"Updating {cfg.prefix}/{key}\n")

    fd, tmp = tempfile.mkstemp(suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            st = os.lstat(target)
            sym = os.readlink(target) if stat_mod.S_ISLNK(st.st_mode) else None
            manifest.write_manifest(f, [(os.path.basename(target), st, sym)])
        _validate_before_publish(entry, tmp)
        if upload:
            assert cfg.store is not None
            cfg.store.put_file(key, tmp, verbose=verbose)
    finally:
        os.unlink(tmp)


def _validate_before_publish(entry: str, path: str) -> None:
    """Run the reader's full validation over a freshly written manifest before
    it is uploaded: no writer may publish a manifest the next download would
    reject (which would brick the entry - every command validates on
    download). Runs on dry-run rehearsals too, so they fail where the real
    push would."""
    try:
        manifest.validate_manifest(path)
    except manifest.ManifestError as e:
        raise manifest.ManifestError(
            f"{entry}: refusing to publish an invalid manifest ({e})"
        ) from e


def download_manifest(cfg: Config, entry: str, dest: str, verbose: bool = False) -> bool:
    assert cfg.store is not None
    # Manifests are small and fetched on nearly every command: a direct
    # GetObject (size unknown -> the direct path) saves s3transfer's HeadObject.
    found = cfg.store.get_object(manifest.manifest_key(entry), dest, verbose=verbose)
    if found:
        manifest.validate_manifest(dest)
    return found


def sync_compare(
    cfg: Config, opts: Opts, entry: str, manifest_path: str | None, sub: str | None = None
) -> PairFilter:
    """Build pull's update-lane strategy (`S3.sync`'s `update_filter`): the
    stat-only streaming ManifestFilter by default, EtagComparison under
    --checksum. `manifest_path=None` (nothing on S3 yet) yields an empty
    filter, so every both-sides pair transfers. The size+mtime-check window is
    resolved for `entry`. (Push builds a `PushJournal` instead, which folds
    the same judgment into its journal emission; pull -u builds an
    ``UpdateFilter`` itself, which also takes the create lane - see cmd_pull.)

    The ManifestFilter streams the manifest file, so the caller must `close()`
    it before unlinking the temp manifest (see cmd_pull)."""
    assert cfg.store is not None
    if opts.checksum:
        return cfg.store.content_compare()
    records: Iterator[tuple[str, ManifestEntry]] = iter(())
    if manifest_path is not None:
        records = manifest.iter_compare_records(manifest_path, sub=sub)
    return manifest.ManifestFilter(records, window_ns=cfg.window_ns_for(entry))


PULL_DOWNLOADED = manifest.PULL_DOWNLOADED
PULL_KEPT = manifest.PULL_KEPT


class UpdateFilter(manifest.ManifestFilter):
    """pull -u's lanes (docs/sync.md, "the newer side wins"): the
    ``ManifestFilter`` judgment with the record's mtime ordering the two
    sides, for both the update lane (a local regular file x its object) and
    the create lane (an object with nothing the sync could see locally -
    nothing at all, or a symlink or directory, which pull's destination
    listing omits). boto3-s3 decides both lanes serially, in one ascending
    compare-key stream, so the inherited one-record cursor serves both.

    A pair downloads only when the record is the newer side; a newer local
    side is kept in full; a tie with a difference - size (content under
    --checksum), mode, kind, or link target - is a conflict, warned here and
    kept. An unrecorded object over a local path, or an object that no
    longer matches its record, cannot be ordered at all and is a conflict
    too: -u's promise is that it never overwrites newer local work, and
    "unknown" is not "older".

    Every decision the metadata apply must honour is spooled, one JSON line
    per key in stream order: ``D`` for a download (the apply settles the
    file to its record whatever its stamped mtime says, and marks its parent
    directory dirtied), ``K`` for a kept key (the apply leaves it alone -
    a conflict was already reported here). A key the filter judged an
    ordinary match is not spooled; the apply applies the same rule itself
    to whatever it meets unspooled. The spool is a temp file the caller owns
    (created before the sync, unlinked after the apply); ``close()`` flushes
    it along with the manifest handle."""

    def __init__(
        self,
        records: Iterator[tuple[str, ManifestEntry]],
        *,
        window_ns: int,
        spool_path: str,
        outpath: str,
        verbose: bool,
        content: PairFilter | None = None,
        prep_modes: dict[str, int] | None = None,
    ):
        super().__init__(records, window_ns=window_ns)
        self._spool = open(spool_path, "w", encoding="utf-8")
        self._outpath = outpath
        self._verbose = verbose
        self._content = content
        # A Windows pull adds the write bit to every read-only local file
        # before the sync (restore.windows_collect_writable_prep); the mode
        # the rule judges is the one from before that, keyed by absolute path.
        self._prep_modes = prep_modes or {}

    def close(self) -> None:
        super().close()
        self._spool.close()

    def _local_mode(self, local_path: str) -> int | None:
        return self._prep_modes.get(os.path.abspath(local_path))

    def _decide(self, marker: str, compare_key: str) -> None:
        # A key can contain a newline, so a line needs an encoding.
        self._spool.write(json.dumps([marker, compare_key]) + "\n")

    def _keep_newer(self, compare_key: str, local_path: str) -> bool:
        self._decide(PULL_KEPT, compare_key)
        if self._verbose:
            console.diag(f"skip (local is newer): {local_path}\n")
        return False

    def _conflict(self, reason: str, compare_key: str, local_path: str) -> bool:
        warn_conflict(reason, local_path)
        self._decide(PULL_KEPT, compare_key)
        return False

    def __call__(self, pair: SyncPair) -> bool:
        if pair.transfer_type.value != "download":
            raise ValueError(f"UpdateFilter judges download pairs only: {pair.compare_key!r}")
        local, remote = pair.dest, pair.src
        key = pair.compare_key
        local_path = local.key.replace("/", os.sep)
        m = self._lookup(key)
        if m is None:
            return self._conflict(CONFLICT_UNRECORDED, key, local_path)
        if not m.is_file:
            # A recorded directory or symlink shadowed by an object at its key:
            # the record is what pull restores, and the apply does that.
            return False
        if remote.size != m.size:
            return self._conflict(CONFLICT_OBJECT, key, local_path)
        st = local.stat_result if isinstance(local, LocalFileInfo) else None
        if st is None:
            try:
                st = os.lstat(local_path)
            except OSError:
                self._decide(PULL_DOWNLOADED, key)
                return True  # vanished between listing and compare; let sync warn
        side = ordered_side(m, st, self.window_ns)
        if side == "local":
            return self._keep_newer(key, local_path)
        differs = (
            self._content(pair)
            if self._content is not None
            else not m.matches_stat(st, self.window_ns)
        )
        if side == "record":
            if not differs:
                # --checksum, content-equal: nothing to download; the apply
                # gives the file its recorded (newer) mtime and mode.
                return False
            self._decide(PULL_DOWNLOADED, key)
            return True
        reason = tie_conflict(
            m,
            st,
            None,
            differs=(CONFLICT_CONTENT if self._content is not None else CONFLICT_SIZE)
            if differs
            else None,
            local_mode=self._local_mode(local_path),
        )
        if reason is not None:
            return self._conflict(reason, key, local_path)
        return False

    def create(self, info: FileInfo) -> bool:
        """The create lane: an object the destination listing paired with
        nothing. Nothing local downloads; a local symlink where a file is
        recorded is the type change the rule orders by the link's own mtime
        (a tie where link mtimes cannot be compared); a local directory is
        never ordered against a file (the two are not a pair, docs/sync.md)
        and takes the ordinary lane."""
        key = info.compare_key
        assert key is not None  # the sync stamps every listed entry
        m = self._lookup(key)
        local_path = os.path.join(self._outpath, key.replace("/", os.sep))
        try:
            st: os.stat_result | None = os.lstat(local_path)
        except OSError:
            st = None
        if st is None:
            if m is not None and not m.is_file:
                return False  # residue at a recorded dir/link key: the apply restores the record
            self._decide(PULL_DOWNLOADED, key)
            return True
        if m is None:
            return self._conflict(CONFLICT_UNRECORDED, key, local_path)
        if not m.is_file:
            return False
        if not stat_mod.S_ISLNK(st.st_mode):
            self._decide(PULL_DOWNLOADED, key)
            return True
        side = ordered_side(m, st, self.window_ns)
        if side == "local":
            return self._keep_newer(key, local_path)
        if side == "record":
            self._decide(PULL_DOWNLOADED, key)
            return True
        return self._conflict(tie_conflict(m, st, None) or CONFLICT_TYPE, key, local_path)


@dataclass
class _DirFrame:
    """An open directory-record delete candidate (docs/journal.md).

    Its no-change placeholder line is already in the journal at ``offset``;
    the pop - once the ascending stream has left the subtree - decides
    whether to flip that line's marker to a drop. ``kept`` is set the moment
    anything beneath survives: the directory record carries the metadata the
    surviving records' restore settles into, so it travels with them rather
    than being asked about on its own."""

    prefix: str  # the dir record's sort key ("sub/"): prefixes every descendant key
    rel: str  # entry-rooted display path ("sub")
    entry: ManifestEntry
    offset: int  # byte offset of the placeholder's marker in the journal
    kept: bool = False  # a record beneath survived: keep silently, never ask


class PushJournal:
    """The single-scan push's compare, journal emitter, and keep/drop policy.

    One instance observes one push sync (docs/journal.md): wired as the
    sync's one pair filter (``decide``), it advances a cursor over the old
    manifest in lockstep with the ascending pair stream - the sync's
    complete-view local walk union the S3 listing - decides what happens to
    each pair, and records every manifest change as one journal line: ``+`` /
    ``!`` carry the new record built from the walked lstat (the stats the
    compare judged), ``-`` the dropped old record verbatim. Every policy
    decision is made here, so ``manifest.merge_journal`` is a pure apply; a
    journal with no real event (``has_events`` False - no-change placeholder
    lines do not count) IS the no-op decision - nothing to rewrite.

    The cursor sees every local item (update and create cover the whole walk)
    and, on a ``--delete`` run, every S3 orphan, so a record it skips over has
    nothing at its key on either side. Under ``--delete`` those skip-overs
    are the record-retirement candidates: a stale file record drops silently
    (its object is already gone), a symlink or special-file record is
    confirmed on arrival through ``record_delete``, and a directory record
    becomes an ancestor-stack frame - the same post-order pull ``--delete``
    uses for local extras - asked only once the stream proves everything
    beneath it resolved deleted, and kept silently (no question) when any
    record beneath survived. Pair decisions are serial and ascending (the
    boto3-s3 contract the design relies on), which is also why
    ``--checksum``'s content comparison runs inline here rather than on a
    pool.

    ``sub`` scopes a sub-path push: the pair stream's compare keys are
    sub-relative, the cursor and the journal stay entry-rooted, and skip-over
    drops apply only inside the replaced range.
    """

    def __init__(
        self,
        journal_path: str,
        old_manifest: str | None,
        *,
        window_ns: int,
        walker: localwalk.ManifestWalker,
        sub: str | None = None,
        content: PairFilter | None = None,
        dest_listed: bool = False,
        delete_mode: bool = False,
        object_delete: FileFilter | None = None,
        record_delete: Callable[[str, ManifestEntry], bool] | None = None,
        update: bool = False,
        verbose: bool = False,
    ) -> None:
        # Binary, because a confirmed directory-record drop seeks back and
        # overwrites its placeholder's one-byte marker in place (see
        # _resolve_frame); a text handle would make those offsets opaque.
        self._out = open(journal_path, "wb")
        try:
            self._records: Iterator[tuple[str, str, ManifestEntry, str]] = (
                manifest.iter_manifest_raw(old_manifest) if old_manifest is not None else iter(())
            )
            self._head = next(self._records, None)
        except BaseException:
            # A corrupt old manifest (validated at download, so only rot or a
            # race) must not leak the journal handle - close() is never
            # reached for a half-built emitter.
            self._out.close()
            raise
        self._window_ns = window_ns
        self._walker = walker
        self._sub = sub
        self._content = content
        # -u: the newer side wins (docs/sync.md). A recorded pair copies only
        # when the local side is the newer one; a newer record keeps its line
        # untouched, and a tie with a difference is a conflict, warned and
        # left alone. Skips print under -v only.
        self._update = update
        self._verbose = verbose
        # Whether this run's pair stream covers every S3 object in the
        # journal's range - true when the push runs a directory sync, false
        # for a single-file or symlink sub-path push, which lists nothing.
        # Only then does "the stream never keyed this record" prove that the
        # record's object is gone (see _skip_over).
        self._dest_listed = dest_listed
        self._delete_mode = delete_mode
        # The per-orphan decision for an S3 object with no local counterpart
        # (the --delete confirmation, or the --yes / dry-run gate). None is a
        # run without --delete: every orphan is still observed - the cursor
        # needs it - and kept. Assert rather than fall back: a delete_mode run
        # that arrived here with no decision would keep every orphan silently,
        # turning an unattended --yes push into a no-op instead of failing.
        assert not (delete_mode and object_delete is None), (
            "a --delete run must carry a per-orphan decision"
        )
        self._object_delete = object_delete
        self._record_delete = record_delete
        # Open directory-record delete candidates, innermost last - the same
        # ancestor-stack post-order pull --delete uses for local extras, so
        # memory stays bounded by directory depth, never tree size.
        self._frames: list[_DirFrame] = []
        self._closed = False
        self.events = 0
        # Record-only candidates the completeness gate suppressed before any
        # question could be asked; folded into the kept-candidates warning
        # (object candidates are counted at the lane's own gate check).
        self.refused_records = 0
        # Kind-conflict pairs seen under --delete: a local non-file occupies a
        # key holding a real S3 object, so the object pairs (update lane)
        # instead of orphaning (delete lane). Offered out-of-lane after the
        # sync (entry-relative key, whether a file record owned the object) -
        # spooled to disk (_pending_object_deletes_spool), not held in a
        # list, so memory stays independent of tree size even in the
        # pathological case (a huge tree entirely replaced by symlinks).
        # This counter is the only in-memory trace; iter_pending_object_deletes
        # replays the spool.
        self.pending_object_deletes = 0
        self._pending_object_deletes_spool: IO[str] | None = None

    @property
    def has_events(self) -> bool:
        return self.events > 0

    # --- key/path mapping ---------------------------------------------------
    def _full_key(self, compare_key: str, *, is_dir: bool) -> str:
        """Pair-stream compare key -> entry-rooted sort key. A sub-path sync's
        stream is sub-relative and keys its own root as ``""``."""
        if self._sub is None:
            return compare_key
        if not compare_key:
            return f"{self._sub}/" if is_dir else self._sub
        return f"{self._sub}/{compare_key}"

    @staticmethod
    def _record_path(full_key: str, *, is_dir: bool) -> str:
        if not full_key:
            return "."
        rel = full_key[:-1] if is_dir else full_key
        return f"./{rel}"

    # --- the cursor ---------------------------------------------------------
    def _advance(self, key: str) -> tuple[ManifestEntry, str] | None:
        """Advance the cursor to ``key``: records ordered before it are
        skip-overs (see ``_skip_over``), an exact hit is consumed and
        returned as ``(entry, raw_line)``. Every key that passes through here
        does so in ascending order, which is what lets an open directory
        frame pop the moment a key outside its subtree appears."""
        while self._head is not None and self._head[0] < key:
            record = self._head
            self._pop_frames(record[0])
            self._skip_over(record)
            self._head = next(self._records, None)
        self._pop_frames(key)
        if self._head is not None and self._head[0] == key:
            _key, _rel, e, line = self._head
            self._head = next(self._records, None)
            return e, line
        return None

    def _skip_over(self, record: tuple[str, str, ManifestEntry, str]) -> None:
        """A record the pair stream never keyed: nothing exists at its key on
        either side (an S3 object would have formed a delete-lane pair, a
        local item an update/create pair).

        A **file** record here has no object left, so it restores nothing:
        every push drops it, ``--delete`` or not, and without a question.
        Retiring it is repair, not deletion - there is no backup at the key
        to protect - and it is the self-heal for a push interrupted (or
        aborted by q) after some of its deletions had already run. This is
        why an S3 orphan is observed even without ``--delete``
        (``_decide_orphan``): a record whose object is still there must reach
        the journal through that pair instead of arriving here.
        Where the run lists no objects at all (``dest_listed`` false - a
        single-file or symlink sub-path push), nothing is provably stale and
        the record is kept.

        Every other kind IS the backup at its key, so only a ``--delete`` run
        retires it: a directory record opens a frame whose drop is decided
        post-order (see ``_resolve_frame``), and a symlink or special-file
        record is confirmed on arrival through ``record_delete``. ``--yes``
        is not a separate lane: it auto-confirms the same candidates through
        the same paths without prompting. All of it only while the scan is
        complete (a record the walk may simply have failed to see is not
        stale) - a gated or kept record pins every open ancestor frame.

        On a sub-path push only records strictly BELOW ``sub`` are
        drop-eligible: the sub sync's S3 listing is slash-bounded, so an
        object at exactly the ``sub`` key (a kind-changed former file) never
        enters any lane - its record is not provably stale and must travel
        with its object. The surviving pair reads as the restorability
        warning until a directory-level ``push --delete`` retires it."""
        key, rel, e, line = record
        if self._sub is not None and not rel.startswith(self._sub + "/"):
            return
        if self._walker.scan_incomplete:
            if self._delete_mode and not e.is_file:
                # A record-only candidate kept without a question: count it,
                # or a record-only run would keep silently with no warning
                # at all. (A gated stale file record is not a candidate -
                # nothing would have been asked - and stays uncounted.)
                self.refused_records += 1
            self._mark_record_kept()
            return
        if e.is_file:
            if self._dest_listed:
                self._emit(manifest.JOURNAL_DROP, line)
            else:
                self._mark_record_kept()
            return
        if not self._delete_mode:
            self._mark_record_kept()
            return
        if e.is_dir:
            # Claim the drop's line position now (the journal is strictly
            # ascending and a directory sorts before its children), as a
            # no-change placeholder; the pop decides whether to flip it.
            offset = self._out.tell()
            self._out.write((manifest.JOURNAL_KEEP + line + "\n").encode("utf-8"))
            self._frames.append(_DirFrame(prefix=key, rel=rel, entry=e, offset=offset))
            return
        if self._record_delete is not None and self._record_delete(rel, e):
            self._emit(manifest.JOURNAL_DROP, line)
        else:
            self._mark_record_kept()

    # --- directory-record frames ---------------------------------------------
    def _mark_record_kept(self) -> None:
        """A record survives beneath the innermost open directory frame:
        the directory record travels with it (it carries the metadata the
        surviving record's restore settles into), so the frame resolves
        silently kept - matching pull --delete, where keeping an item keeps
        every extra directory still open above it without a question of its
        own."""
        if self._frames:
            self._frames[-1].kept = True

    def _pop_frames(self, key: str) -> None:
        """Resolve every open frame whose subtree the ascending stream has
        left (``key`` no longer extends its prefix), innermost first."""
        while self._frames and not key.startswith(self._frames[-1].prefix):
            self._resolve_frame(self._frames.pop())

    def _resolve_frame(self, frame: _DirFrame) -> None:
        """The post-order decision for a popped directory-record candidate:
        everything beneath it resolved deleted, so ask; confirmed, flip the
        placeholder's marker byte to a drop (the line's position and payload
        are already correct). A kept frame - anything beneath survived, the
        answer was no, or the completeness gate refused (``record_delete``
        checks it) - leaves the no-change line as is and pins its parent."""
        if (
            frame.kept
            or self._record_delete is None
            or not self._record_delete(frame.rel, frame.entry)
        ):
            self._mark_record_kept()
            return
        end = self._out.tell()
        self._out.seek(frame.offset)
        self._out.write(manifest.JOURNAL_DROP.encode("utf-8"))
        self._out.seek(end)
        self.events += 1

    # --- journal output -----------------------------------------------------
    def _emit(self, marker: str, payload: str) -> None:
        self._out.write((marker + payload + "\n").encode("utf-8"))
        self.events += 1

    def _emit_new(self, marker: str, path: str, st: os.stat_result, sym: str | None) -> None:
        self._emit(marker, manifest.format_entry(path, st, sym))

    def _gap(self, body: str) -> None:
        """A walk gap discovered at decision time (an unreadable file, a
        symlink racing away): warn like the walk itself would and mark the
        scan incomplete, gating deletions exactly like a walk-time gap."""
        self._walker.scan_incomplete = True
        _walk_warning(body)

    def _probe_readable(self, info: FileInfo) -> bool:
        """The transfer view open-probes every file up front; the complete
        view probes none, so probe just the files a lane decided to copy. An
        unreadable one warn-skips (exit 2) instead of failing the transfer -
        today's warn-and-continue, at the cost of probing only changed
        files."""
        try:
            with open(info.key.replace("/", os.sep), "rb"):
                return True
        except OSError:
            self._gap(f"Skipping file {info.key}. File/Directory is not readable.")
            return False

    def _record_pending_object_delete(self, key: str, recorded: bool) -> None:
        """Spool one kind-conflict delete candidate to disk (JSON-encoded,
        one per line - a key can contain a newline, so round-tripping it
        needs an encoding, not just a delimiter). Lazily opened on first use:
        an anonymous, auto-deleting temp file that also works on Windows.
        Lane decisions are serial and ascending (the class docstring), and
        this is their only writer, so no lock is needed."""
        if self._pending_object_deletes_spool is None:
            self._pending_object_deletes_spool = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self._pending_object_deletes_spool.write(json.dumps([key, recorded]) + "\n")
        self.pending_object_deletes += 1

    def iter_pending_object_deletes(self) -> Iterator[tuple[str, bool]]:
        """Replay the spooled kind-conflict delete candidates: both call
        sites check ``pending_object_deletes`` only after ``close()`` (see
        its docstring), so the spool must outlive close() and this is where
        it finally goes - fully drained or the generator abandoned early,
        the spool is closed (which also deletes it) and forgotten either
        way."""
        spool = self._pending_object_deletes_spool
        if spool is None:
            return
        try:
            spool.flush()
            spool.seek(0)
            for line in spool:
                key, recorded = json.loads(line)
                yield key, recorded
        finally:
            spool.close()
            self._pending_object_deletes_spool = None

    # --- the pair filter ----------------------------------------------------
    def decide(self, pair: MergedPair) -> bool:
        """The sync's one decision (``S3.sync``'s ``pair_filter``): every
        merged pair, whatever its shape, serially and in ascending compare-key
        order on the sync's own thread. ``True`` takes that pair's default
        action - upload a new or changed file, delete an orphan.

        One entry point for all three shapes is what keeps the cursor in
        lockstep with the stream: a record the stream never keys is provably
        stale (``_skip_over``), which only holds if every key on either side
        passes through here - including the S3 orphans of a run with no
        ``--delete``, whose decision is then a flat no."""
        if isinstance(pair, SyncPair):
            return self._decide_update(pair)
        if isinstance(pair, SrcOnlyPair):
            return self._decide_create(pair.src)
        return self._decide_orphan(pair.dest)

    def _decide_update(self, pair: SyncPair) -> bool:
        """A both-sides pair: a local item x the S3 object at its key."""
        assert pair.transfer_type.value == "upload"
        src = pair.src
        assert isinstance(src, LocalFileInfo)  # the pair's local side carries the lstat
        st = src.stat_result
        assert st is not None and src.compare_key is not None
        is_dir = stat_mod.S_ISDIR(st.st_mode)
        key = self._full_key(src.compare_key, is_dir=is_dir)
        old = self._advance(key)
        if is_dir or not stat_mod.S_ISREG(st.st_mode):
            # Kind conflict: the local non-file shields the object from the
            # delete lane (it pairs instead of orphaning), so record the local
            # side as usual and offer the object out-of-lane under --delete.
            # Under -u the record may win instead (a newer record, or a tie);
            # the object then stays what the kept record does not describe,
            # and is not offered - nothing local replaced it.
            recorded = self._journal_nonfile(key, st, src, old)
            if self._delete_mode and (recorded or not self._update):
                self._record_pending_object_delete(key, old is not None and old[0].is_file)
            return False
        e = old[0] if old is not None else None
        if self._update and e is not None:
            return self._decide_update_newer(pair, src, st, key, e)
        if self._content is not None:
            # The content compare reads the file, and an unreadable one would
            # abort the sync with AccessDenied from inside the hash - probe
            # first so it warn-skips (exit 2) like every other gap. The extra
            # open is noise next to the read the comparison does anyway.
            if not self._probe_readable(src):
                return False
            copy = self._content(pair)
        else:
            copy = (
                e is None
                or not e.is_file
                or pair.dest.size != e.size  # the remote drifted; size is free evidence
                or not e.matches_stat(st, self._window_ns)
            )
            if copy and not self._probe_readable(src):
                return False
        if copy:
            self._emit_new(
                manifest.JOURNAL_ADD if old is None else manifest.JOURNAL_REPLACE,
                self._record_path(key, is_dir=False),
                st,
                None,
            )
            return True
        # No transfer, but the record may still be stale: a mode drift (the
        # predicate status shares), or - under --checksum, whose ETag decision
        # skips the transfer on content-equal files - a missing/non-file record
        # or a size+mtime drift outside the window. Refreshing the record there
        # keeps --checksum's self-healing on par with the default push (which
        # re-transfers and refreshes). A within-window mtime drift stays
        # untouched: the window is a rounding tolerance, never snapped. In the
        # default path this branch only runs when matches_stat already held (why
        # copy was False), so the extra check is a no-op there.
        if (
            e is None
            or not e.is_file
            or mode_differs(e, st)
            or not e.matches_stat(st, self._window_ns)
        ):
            self._emit_new(
                manifest.JOURNAL_ADD if old is None else manifest.JOURNAL_REPLACE,
                self._record_path(key, is_dir=False),
                st,
                None,
            )
        return False

    def _skip_newer(self, info: FileInfo) -> None:
        """-u left a pair alone because the backup is the newer side: not an
        event (nothing happened), so it prints under -v only, like a request
        trace - a quiet push still means "nothing to do"."""
        if self._verbose:
            console.diag(f"skip (backup is newer): {info.key}\n")

    def _decide_update_newer(
        self,
        pair: SyncPair,
        src: LocalFileInfo,
        st: os.stat_result,
        key: str,
        e: ManifestEntry,
    ) -> bool:
        """The -u verdict for a local regular file whose key holds both an S3
        object and a record (docs/sync.md, "the newer side wins"): copy and
        re-record only when the local side is the newer one; a newer record
        keeps its line untouched, whatever the local file looks like; a tie
        is a conflict when anything differs - size (content under
        --checksum), mode, or the recorded kind - and a no-op otherwise. An
        object that no longer matches its record cannot be ordered at all
        (the record describes neither side), so it is a conflict too."""
        if e.is_file and pair.dest.size != e.size:
            warn_conflict(CONFLICT_OBJECT, src.key)
            return False
        side = ordered_side(e, st, self._window_ns)
        if side == "record":
            self._skip_newer(src)
            return False
        if side == "local":
            if not self._probe_readable(src):
                return False
            copy = self._content is None or self._content(pair)
            # A newer local side re-records either way: under --checksum a
            # content-equal file still carries the newer mtime and mode, the
            # same refresh the default push's re-transfer gives it.
            self._emit_new(manifest.JOURNAL_REPLACE, self._record_path(key, is_dir=False), st, None)
            return copy
        # A tie: the rule has no side to pick, so any difference is a conflict.
        differs: str | None = None
        if not e.is_file:
            pass  # the kind differs: tie_conflict reports that first
        elif self._content is not None:
            if not self._probe_readable(src):
                return False
            if self._content(pair):
                differs = CONFLICT_CONTENT
        elif not e.matches_stat(st, self._window_ns):
            differs = CONFLICT_SIZE
        reason = tie_conflict(e, st, None, differs=differs)
        if reason is not None:
            warn_conflict(reason, src.key)
        return False

    def _decide_create(self, info: FileInfo) -> bool:
        """A local-only item - a new file, or any directory / symlink / special
        file (objectless, so never paired), the root included (compare key
        ``""``, the stream's first item)."""
        assert isinstance(info, LocalFileInfo)  # a source-only pair is the local walk
        st = info.stat_result
        assert st is not None and info.compare_key is not None
        is_dir = stat_mod.S_ISDIR(st.st_mode)
        key = self._full_key(info.compare_key, is_dir=is_dir)
        old = self._advance(key)
        if is_dir or not stat_mod.S_ISREG(st.st_mode):
            self._journal_nonfile(key, st, info, old)
            return False
        if self._update and old is not None and not old[0].is_file:
            # A regular file where a symlink or special file is recorded (a
            # directory sorts to another key and never pairs): the kind
            # change follows the rule like its mirror image in
            # _journal_nonfile_newer - a newer record keeps, a tie conflicts.
            side = ordered_side(old[0], st, self._window_ns)
            if side == "record":
                self._skip_newer(info)
                return False
            if side == "tie":
                warn_conflict(tie_conflict(old[0], st, None) or CONFLICT_TYPE, info.key)
                return False
        # A local regular file with no S3 object: upload it - the create lane
        # copies every new file, including a recorded file whose object went
        # missing (the self-heal).
        if not self._probe_readable(info):
            return False
        self._emit_new(
            manifest.JOURNAL_ADD if old is None else manifest.JOURNAL_REPLACE,
            self._record_path(key, is_dir=False),
            st,
            None,
        )
        return True

    def _decide_orphan(self, info: FileInfo) -> bool:
        """An S3-only key - the delete candidate, put to ``object_delete``
        (the --delete confirmation, or the --yes/dry-run gate; a run without
        --delete has none and keeps every orphan).

        A confirmed deletion drops the owning file record with its object -
        the two travel together. A non-file record at the key (objectless by
        definition) is never dropped by a confirmation, and an unrecorded
        object has nothing to drop. Any record that survives here - an object
        answered n, or a non-file record shadowed by an object at its key -
        pins every open ancestor frame (an unrecorded object does not: only
        records constrain the manifest's directory-parent rule)."""
        assert info.compare_key is not None
        old = self._advance(self._full_key(info.compare_key, is_dir=False))
        doomed = self._object_delete is not None and self._object_delete(info)
        if old is not None:
            if doomed and old[0].is_file:
                self._emit(manifest.JOURNAL_DROP, old[1])
            else:
                self._mark_record_kept()
        return doomed

    def _journal_nonfile(
        self,
        key: str,
        st: os.stat_result,
        info: FileInfo,
        old: tuple[ManifestEntry, str] | None,
    ) -> bool:
        """Journal a directory / symlink / special-file item: an event only
        when the record would change - a new path, a changed symlink target
        or (where ``SYMLINK_MTIME_SUPPORTED``) an out-of-window symlink mtime
        drift, an out-of-window directory or special-file mtime drift, a mode
        drift (directories and specials; symlink permission bits are compared
        nowhere), or a type change. Returns whether an event was journaled.

        Under -u the item's own mtime orders the two sides instead: a newer
        local side re-records, a newer record is kept, and a tie is a
        conflict when the target, kind, or mode differs. A symlink on a
        platform that cannot set link mtimes (Windows) has no mtime to order
        by, so a changed target there is always a conflict."""
        is_dir = stat_mod.S_ISDIR(st.st_mode)
        path = self._record_path(key, is_dir=is_dir)
        sym: str | None = None
        if stat_mod.S_ISLNK(st.st_mode):
            try:
                sym = os.readlink(info.key.replace("/", os.sep))
            except OSError:
                # Raced away between the scan and here: no record can be
                # built, so the walk did not see the whole tree.
                self._gap(f"Skipping file {info.key}. File changed during the walk.")
                return False
        if old is None:
            self._emit_new(manifest.JOURNAL_ADD, path, st, sym)
            return True
        e = old[0]
        if self._update:
            return self._journal_nonfile_newer(path, st, info, sym, e)
        if stat_mod.S_ISLNK(st.st_mode):
            changed = e.sym_target != sym or (
                SYMLINK_MTIME_SUPPORTED
                and (e.mtime_ns is None or abs(st.st_mtime_ns - e.mtime_ns) > self._window_ns)
            )
        elif is_dir:
            # a dir key always holds a dir record; its own mtime drift is
            # tracked the same as a special file's - see the else branch.
            changed = (
                mode_differs(e, st)
                or e.mtime_ns is None
                or abs(st.st_mtime_ns - e.mtime_ns) > self._window_ns
            )
        else:
            # A special file (FIFO, device): a type change, a mode drift, or
            # an out-of-window own-mtime drift (tracked the same as a
            # directory's or symlink's), so status's M settles and pull
            # restores the current mtime instead of a stale one.
            changed = (
                stat_mod.S_IFMT(e.mode) != stat_mod.S_IFMT(st.st_mode)
                or mode_differs(e, st)
                or e.mtime_ns is None
                or abs(st.st_mtime_ns - e.mtime_ns) > self._window_ns
            )
        if changed:
            self._emit_new(manifest.JOURNAL_REPLACE, path, st, sym)
        return changed

    def _journal_nonfile_newer(
        self,
        path: str,
        st: os.stat_result,
        info: FileInfo,
        sym: str | None,
        e: ManifestEntry,
    ) -> bool:
        """The -u half of ``_journal_nonfile`` for a recorded item."""
        side = ordered_side(e, st, self._window_ns)
        if side == "local":
            self._emit_new(manifest.JOURNAL_REPLACE, path, st, sym)
            return True
        if side == "record":
            self._skip_newer(info)
            return False
        reason = tie_conflict(e, st, sym)
        if reason is not None:
            warn_conflict(reason, info.key)
        return False

    # --- explicit events (sub-path pushes) ----------------------------------
    def record_root(self, st: os.stat_result) -> None:
        """The entry root's ``.`` record for a first-ever manifest born from a
        sub-path push (no old manifest, so the cursor is empty)."""
        self._emit_new(manifest.JOURNAL_ADD, ".", st, None)

    def record_ancestor(self, rel: str, st: os.stat_result, local_path: str) -> None:
        """A sub-path push's parent directory: journal only a drift (a missing
        record, or a mode/mtime change) - the ancestors' metadata should
        restore on pull, but re-recording an unchanged ancestor was the old
        pipeline's walked-path-wins artifact, not a requirement (and an
        excluded ancestor is not journaled at all - a parent record is
        optional, docs/excludes.md). A directory's own mtime is
        tracked like _journal_nonfile's dir branch, so a sub-path push that
        changed the ancestor's mtime (adding the new child does) refreshes it
        instead of leaving a stale value for pull to restore."""
        old = self._advance(rel + "/")
        if old is None:
            self._emit_new(manifest.JOURNAL_ADD, f"./{rel}", st, None)
        elif self._update:
            # The newer side wins here too; a kept (newer) ancestor record is
            # not worth a skip line - the ancestors are incidental to the
            # sub-path named on the command line.
            side = ordered_side(old[0], st, self._window_ns)
            if side == "local":
                self._emit_new(manifest.JOURNAL_REPLACE, f"./{rel}", st, None)
            elif side == "tie":
                reason = tie_conflict(old[0], st, None)
                if reason is not None:
                    warn_conflict(reason, local_path)
        elif (
            mode_differs(old[0], st)
            or old[0].mtime_ns is None
            or abs(st.st_mtime_ns - old[0].mtime_ns) > self._window_ns
        ):
            self._emit_new(manifest.JOURNAL_REPLACE, f"./{rel}", st, None)

    def record_target(self, rel: str, st: os.stat_result, sym_target: str | None) -> None:
        """An explicitly pushed file or symlink sub-path always re-records:
        naming the path is the instruction to back up its current state."""
        old = self._advance(rel)
        self._emit_new(
            manifest.JOURNAL_ADD if old is None else manifest.JOURNAL_REPLACE,
            f"./{rel}",
            st,
            sym_target,
        )

    def close(self) -> None:
        """Drain the cursor (trailing records are skip-overs), resolve the
        directory frames still open (the stream ending is the proof their
        subtrees hold nothing more), and flush the journal so it can be
        merged. Idempotent - a second call returns at once, even after a
        first call that raised. The journal handle closes even when a
        confirmation aborts mid-drain (q raises), so the file can always be
        unlinked. Leaves the pending-object-delete spool untouched - both
        call sites read it only after close(), through
        iter_pending_object_deletes, which owns its lifetime."""
        if self._closed:
            return
        try:
            while self._head is not None:
                record = self._head
                self._pop_frames(record[0])
                self._skip_over(record)
                self._head = next(self._records, None)
            while self._frames:
                self._resolve_frame(self._frames.pop())
        finally:
            self._closed = True
            closer = getattr(self._records, "close", None)
            if callable(closer):
                closer()
            self._out.close()


def publish_journal_manifest(
    cfg: Config, entry: str, old_manifest: str | None, journal_path: str, opts: Opts
) -> None:
    """Apply a non-empty push journal to the old manifest and upload the
    result. The merge streams to a local temp file and is validated before
    publishing, like every manifest write; a dry run computes and validates
    the merge for real - surfacing the same warnings - and skips only the S3
    write."""
    key = manifest.manifest_key(entry)
    fd, tmp = tempfile.mkstemp(suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            manifest.merge_journal(f, old_manifest, journal_path, warn=console.warn)
        _validate_before_publish(entry, tmp)
        if opts.dryrun:
            console.out(f"(dry-run) would update manifest: {key}\n")
        else:
            console.diag(f"Updating {cfg.prefix}/{key}\n")
            assert cfg.store is not None
            cfg.store.put_file(key, tmp, verbose=opts.verbose)
    finally:
        os.unlink(tmp)


def drop_subtree_records(
    cfg: Config, entry: str, old_manifest: str | None, sub: str, opts: Opts
) -> bool:
    """Remove the manifest records at/under ``sub`` after a confirmed
    subtree deletion (``push --delete entry/gone-sub``): a journal of ``-``
    events for the range, merged and republished. Returns whether the
    manifest changed (False when nothing was recorded there, or the entry has
    no manifest)."""
    if old_manifest is None:
        return False
    fd, journal_path = tempfile.mkstemp(suffix=".journal")
    try:
        dropped = 0
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            for _key, rel, _e, line in manifest.iter_manifest_raw(old_manifest):
                if rel == sub or rel.startswith(sub + "/"):
                    out.write(manifest.JOURNAL_DROP + line + "\n")
                    dropped += 1
        if not dropped:
            return False
        publish_journal_manifest(cfg, entry, old_manifest, journal_path, opts)
        return True
    finally:
        os.unlink(journal_path)


def download_from_s3(
    cfg: Config,
    entry: str,
    outpath: str,
    is_dir: bool,
    verbose: bool,
    sub: str | None = None,
    compare: PairFilter | None = None,
    create: bool | FileFilter = True,
    size: int | None = None,
    dryrun: bool = False,
) -> tuple[int, bool]:
    assert cfg.store is not None
    rel = f"{entry}/{sub}" if sub else entry

    if is_dir:
        # S3.sync creates a missing local destination before it lists anything
        # (aws-cli parity), even on a dry run. pull --dry-run promises to change
        # nothing, so note what is missing now and rmdir it afterwards - rmdir
        # only removes empty directories, so anything real is left alone.
        created: list[str] = []
        if dryrun:
            probe = os.path.abspath(outpath)
            while not os.path.exists(probe):
                created.append(probe)
                parent = os.path.dirname(probe)
                if parent == probe:
                    break
                probe = parent
        try:
            result = cfg.store.sync_down(
                rel, outpath, compare=compare, create=create, dryrun=dryrun, verbose=verbose
            )
        finally:
            for path in created:  # leaf-first, so each rmdir empties its parent
                try:
                    os.rmdir(path)
                except OSError:
                    break
        if result.returncode != 0:
            return result.returncode, False
        return 0, result.results > 0

    # Single file: a transfer always happens (we only reach here on a manifest
    # mismatch), so a successful download counts as changed - which keeps the
    # dry-run stand-in line ("would apply manifest metadata") printed for it.
    # `size` (from the manifest record) routes a
    # large file through multipart download; a small one is a direct GetObject.
    # The lane is named on the transfer line: a directory sync's lines come
    # from boto3-s3, which reports its own transfers, but nothing else would
    # say how this one object travelled.
    lane = "boto3-s3 cp" if cfg.store.multipart_download(size) else "boto3-s3 get_file"
    if dryrun:
        # The download writes the local file, so it is a mutation and stays
        # skipped. No substitute probe either: a HeadObject can succeed or
        # fail under different IAM permissions than the real GetObject, and a
        # dry run must only make the calls the real run would make.
        console.out(f"(dry-run) download: {cfg.prefix}/{rel} to {outpath} ({lane})\n")
        return 0, True
    if not cfg.store.get_object(rel, outpath, size=size, verbose=verbose):
        # A recorded object that is gone is stale residue, not this pull's
        # reason to abort: warn here - the one place that knows the object
        # is missing rather than merely "nothing local" - and return clean
        # with changed=False, which tells cmd_pull to skip this record in
        # full. Whatever is local stays untouched: applying the record's
        # metadata over content that was never restored would report a
        # restore that did not happen.
        console.warn(
            f"warning: no data object behind this record - skipped"
            f" (a push retires the stale record): {cfg.prefix}/{rel}"
        )
        return 0, False
    # Reported after the fact, like the sync's own lines: a line printed up
    # front would announce a download the stale-record branch above never made.
    console.out(f"download: {cfg.prefix}/{rel} to {outpath} ({lane})\n")
    return 0, True
