# Requires Python 3.10+
"""The manifest <-> S3 bridge and download orchestration.

Writes v3 manifests to S3 (the journal merge, the ``--meta-only`` rewrite,
and the sub-tree patch), downloads a manifest or a data tree, and owns the
push's journal emitter (``PushJournal``) and pull's compare strategy. This is
the seam between the pure manifest format (manifest.py), the S3 backend
(store.py), and the command layer (commands.py).
"""

from __future__ import annotations

import itertools
import os
import stat as stat_mod
import tempfile
from collections.abc import Iterator
from typing import TYPE_CHECKING

from boto3_s3 import LocalFileInfo

from s3bak import localwalk, manifest
from s3bak.compare import SYMLINK_MTIME_SUPPORTED, mode_differs
from s3bak.config import Config, Opts
from s3bak.console import err, note_warning, write_output, write_stderr
from s3bak.manifest import ManifestEntry

if TYPE_CHECKING:
    from boto3_s3 import FileFilter, FileInfo, PairFilter, SyncPair


def _walk_warning(body: str) -> None:
    """The manifest walk's warn hook: boto3-s3 message bodies -> one warning
    line each (exit 2), aws-cli's own prefix included."""
    note_warning(f"warning: {body}")


def write_manifest_to_aws(
    cfg: Config,
    entry: str,
    target: str,
    excludes: list[str],
    verbose: bool,
    *,
    old_manifest: str | None = None,
    keep_old: bool = False,
    upload: bool = True,
) -> None:
    """Walk `target` in S3 key order, stream the v3 manifest to a temp file,
    and upload it - the ``--meta-only`` rewrite (an ordinary push merges its
    journal instead, see ``publish_journal_manifest``). For a directory entry
    the walk is merged with `old_manifest` under the `keep_old` policy, so
    records of kept-but-locally-vanished files survive the rewrite (see
    manifest.write_merged). A single-file entry has one record and no merge.
    The walk itself warns (exit 2) when it cannot see the whole tree - an
    unreadable directory, a path racing away mid-walk - since the manifest it
    feeds is the record of what the push saw. ``upload=False`` is the dry-run
    preview: the walk and merge run for real - emitting the same warnings as
    a real push - and only the S3 write is skipped."""
    key = manifest.manifest_key(entry)
    if upload:
        write_stderr(f"Updating {cfg.prefix}/{key}\n")

    fd, tmp = tempfile.mkstemp(suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if os.path.isdir(target):
                manifest.write_merged(
                    f,
                    old_manifest,
                    None,
                    localwalk.walk_tree(target, excludes, warn=_walk_warning),
                    keep_old=keep_old,
                    warn=note_warning,
                )
            else:
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


def patch_manifest_subtree(
    cfg: Config,
    entry: str,
    target_root: str,
    sub: str,
    excludes: list[str],
    opts: Opts,
    *,
    keep_old: bool = False,
    old_manifest: str | None = None,
) -> bool:
    """Replace the manifest records under `sub` and re-upload - the
    ``--meta-only`` sub-path rewrite (an ordinary sub-path push journals
    instead, see ``PushJournal``).

    target_root/sub may be a file, a symlink, or a directory. If it does not
    exist locally, the records under `sub` are simply removed. Old and new
    records are both in sort-key order, so this is a streaming merge
    (manifest.write_merged), not a read-all + sort. `old_manifest` is the
    caller's already-validated copy of the current manifest - every sub-path
    push downloads it before mutating S3 - and None means the entry has no
    manifest yet. A dry run computes the whole patch for real - the walk, the
    merge and its warnings - and skips only the S3 write.
    """
    key = manifest.manifest_key(entry)
    if old_manifest is None and not os.path.lexists(target_root):
        # Deleting a never-backed sub-path beneath a root that is gone has
        # no manifest state to update (and no root metadata from which to
        # create a valid directory manifest).
        return False
    fd_new, new_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd_new)  # reopened by name below; closing now avoids an fd leak on error
    try:
        local_sub = os.path.join(target_root, sub)
        new_entries: Iterator[tuple[str, os.stat_result, str | None]] = iter(())
        if os.path.lexists(local_sub):
            # Ancestor directory records for sub's parents: every record needs
            # a recorded directory parent (the validator's rule), and neither
            # a first-ever manifest nor one that predates a newly created
            # nested directory has them. Re-recording an already-recorded
            # ancestor is harmless - a walked path wins the merge, exactly as
            # a full push would re-record it.
            ancestors: list[tuple[str, os.stat_result, str | None]] = []
            acc = target_root
            rel = "."
            for part in sub.split("/")[:-1]:
                acc = os.path.join(acc, part)
                rel = f"{rel}/{part}"
                ancestors.append((rel, os.lstat(acc), None))
            new_entries = itertools.chain(
                ancestors, localwalk.iter_subtree(local_sub, sub, excludes, warn=_walk_warning)
            )
        if old_manifest is None:
            # First-ever manifest for this entry, born from a sub-path push:
            # record the entry root too, so the manifest keeps the dir-entry
            # shape ('.'-rooted) and the root's metadata restores on pull.
            root_record = (".", os.lstat(target_root), None)
            new_entries = itertools.chain([root_record], new_entries)
        with open(new_path, "w", encoding="utf-8") as out:
            manifest.write_merged(
                out,
                old_manifest,
                sub,
                new_entries,
                keep_old=keep_old,
                warn=note_warning,
            )
        _validate_before_publish(entry, new_path)
        if opts.dryrun:
            write_output(f"(dry-run) would patch manifest: {key} (sub={sub})\n")
        else:
            write_stderr(f"Updating {cfg.prefix}/{key}\n")
            assert cfg.store is not None
            cfg.store.put_file(key, new_path, verbose=opts.verbose)
        return True
    finally:
        os.unlink(new_path)


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
    the same judgment into its journal emission.)

    The ManifestFilter streams the manifest file, so the caller must `close()`
    it before unlinking the temp manifest (see cmd_pull)."""
    assert cfg.store is not None
    if opts.checksum:
        return cfg.store.content_compare()
    records: Iterator[tuple[str, ManifestEntry]] = iter(())
    if manifest_path is not None:
        records = manifest.iter_compare_records(manifest_path, sub=sub)
    return manifest.ManifestFilter(records, window_ns=cfg.window_ns_for(entry))


class PushJournal:
    """The single-scan push's compare, journal emitter, and keep/drop policy.

    One instance observes one push sync (docs/journal.md): wired as the
    sync's three lane filters, it advances a cursor over the old manifest in
    lockstep with the ascending pair stream - the sync's complete-view local
    walk union the S3 listing - decides each lane's action, and records every
    manifest change as one journal line: ``+`` / ``!`` carry the new record
    built from the walked lstat (the stats the compare judged), ``-`` the
    dropped old record verbatim. Every policy decision is made here, so
    ``manifest.merge_journal`` is a pure apply; an empty journal
    (``has_events`` False) IS the no-op decision - nothing to rewrite.

    The cursor sees every local item (update and create cover the whole walk)
    and, on a ``--delete`` run, every S3 orphan, so a record it skips over has
    nothing at its key on either side. Lane decisions are serial and
    ascending (the boto3-s3 contract the design relies on), which is also why
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
        delete_mode: bool = False,
        mirror: bool = False,
    ) -> None:
        self._out = open(journal_path, "w", encoding="utf-8")
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
        self._delete_mode = delete_mode
        self._mirror = mirror
        self.events = 0
        # Uploads with no owning file record - the birth (create lane) and
        # re-upload (update lane) faces of an unrecorded object. Read by the
        # push --data-only warning, so it repeats while the object stays
        # unrecorded.
        self.unrecorded_uploads = 0
        # Kind-conflict pairs seen under --delete: a local non-file occupies a
        # key holding a real S3 object, so the object pairs (update lane)
        # instead of orphaning (delete lane). Offered out-of-lane after the
        # sync: (entry-relative key, whether a file record owned the object).
        self.pending_object_deletes: list[tuple[str, bool]] = []

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
        returned as ``(entry, raw_line)``."""
        while self._head is not None and self._head[0] < key:
            self._skip_over(self._head)
            self._head = next(self._records, None)
        if self._head is not None and self._head[0] == key:
            _key, _rel, e, line = self._head
            self._head = next(self._records, None)
            return e, line
        return None

    def _skip_over(self, record: tuple[str, str, ManifestEntry, str]) -> None:
        """A record the pair stream never keyed: nothing exists at its key on
        either side (an S3 object would have formed a delete-lane pair, a
        local item an update/create pair). Keep-by-default keeps it. A
        ``--delete`` run drops a stale file record - its object is already
        gone, the interrupted-deletion self-heal - and the ``--yes`` mirror
        drops every vanished record; both only while the scan is complete
        (a record the walk may simply have failed to see is not stale).

        On a sub-path push only records strictly BELOW ``sub`` are
        drop-eligible: the sub sync's S3 listing is slash-bounded, so an
        object at exactly the ``sub`` key (a kind-changed former file) never
        enters any lane - its record is not provably stale and must travel
        with its object. The surviving pair reads as the restorability
        warning until a directory-level ``push --delete`` retires it."""
        _key, rel, e, line = record
        if not self._delete_mode or self._walker.scan_incomplete:
            return
        if self._sub is not None and not rel.startswith(self._sub + "/"):
            return
        if self._mirror or e.is_file:
            self._emit(manifest.JOURNAL_DROP, line)

    # --- journal output -----------------------------------------------------
    def _emit(self, marker: str, payload: str) -> None:
        self._out.write(marker + payload + "\n")
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

    # --- lane filters -------------------------------------------------------
    def update_filter(self, pair: SyncPair) -> bool:
        """The update lane: a both-sides pair (local item x S3 object)."""
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
            self._journal_nonfile(key, st, src, old)
            if self._delete_mode:
                self.pending_object_deletes.append((key, old is not None and old[0].is_file))
            return False
        e = old[0] if old is not None else None
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
            if e is None or not e.is_file:
                self.unrecorded_uploads += 1
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

    def create_filter(self, info: FileInfo) -> bool:
        """The create lane: a local-only item - a new file, or any directory /
        symlink / special file (objectless, so never paired), the root
        included (compare key ``""``, the stream's first item)."""
        assert isinstance(info, LocalFileInfo)  # the create lane's side is the local walk
        st = info.stat_result
        assert st is not None and info.compare_key is not None
        is_dir = stat_mod.S_ISDIR(st.st_mode)
        key = self._full_key(info.compare_key, is_dir=is_dir)
        old = self._advance(key)
        if is_dir or not stat_mod.S_ISREG(st.st_mode):
            self._journal_nonfile(key, st, info, old)
            return False
        # A local regular file with no S3 object: upload it - the create lane
        # copies every new file, including a recorded file whose object went
        # missing (the self-heal).
        if not self._probe_readable(info):
            return False
        e = old[0] if old is not None else None
        if e is None or not e.is_file:
            self.unrecorded_uploads += 1
        self._emit_new(
            manifest.JOURNAL_ADD if old is None else manifest.JOURNAL_REPLACE,
            self._record_path(key, is_dir=False),
            st,
            None,
        )
        return True

    def observe_delete(self, inner: FileFilter) -> FileFilter:
        """Wrap the delete lane's decision (the --delete confirmation, or the
        mirror/dry-run gate): a confirmed deletion drops the owning file
        record with its object - the two travel together. A non-file record
        at the key (objectless by definition) is never dropped by a
        confirmation, and an unrecorded object has nothing to drop."""

        def decide(info: FileInfo) -> bool:
            assert info.compare_key is not None
            old = self._advance(self._full_key(info.compare_key, is_dir=False))
            doomed = inner(info)
            if doomed and old is not None and old[0].is_file:
                self._emit(manifest.JOURNAL_DROP, old[1])
            return doomed

        return decide

    def _journal_nonfile(
        self,
        key: str,
        st: os.stat_result,
        info: FileInfo,
        old: tuple[ManifestEntry, str] | None,
    ) -> None:
        """Journal a directory / symlink / special-file item: an event only
        when the record would change - a new path, a changed symlink target
        or (where ``SYMLINK_MTIME_SUPPORTED``) an out-of-window symlink mtime
        drift, an out-of-window directory or special-file mtime drift, a mode
        drift (directories and specials; symlink permission bits are compared
        nowhere), or a type change."""
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
                return
        if old is None:
            self._emit_new(manifest.JOURNAL_ADD, path, st, sym)
            return
        e = old[0]
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

    # --- explicit events (sub-path pushes) ----------------------------------
    def record_root(self, st: os.stat_result) -> None:
        """The entry root's ``.`` record for a first-ever manifest born from a
        sub-path push (no old manifest, so the cursor is empty)."""
        self._emit_new(manifest.JOURNAL_ADD, ".", st, None)

    def record_ancestor(self, rel: str, st: os.stat_result) -> None:
        """A sub-path push's parent directory: journal only a drift (a missing
        record, or a mode/mtime change) - every record needs a recorded directory
        parent, but re-recording an unchanged ancestor was the old pipeline's
        walked-path-wins artifact, not a requirement. A directory's own mtime is
        tracked like _journal_nonfile's dir branch, so a sub-path push that
        changed the ancestor's mtime (adding the new child does) refreshes it
        instead of leaving a stale value for pull to restore."""
        old = self._advance(rel + "/")
        if old is None:
            self._emit_new(manifest.JOURNAL_ADD, f"./{rel}", st, None)
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
        """Drain the cursor (trailing records are skip-overs) and flush the
        journal so it can be merged. Idempotent."""
        while self._head is not None:
            self._skip_over(self._head)
            self._head = next(self._records, None)
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
            manifest.merge_journal(f, old_manifest, journal_path, warn=note_warning)
        _validate_before_publish(entry, tmp)
        if opts.dryrun:
            write_output(f"(dry-run) would update manifest: {key}\n")
        else:
            write_stderr(f"Updating {cfg.prefix}/{key}\n")
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


def _print_transfer_lines(stdout: str) -> bool:
    """Print the transfer-result lines. Returns True if any line was printed.
    ``stdout`` is TransferResult.stdout: the store builds it line by line from
    on_result callbacks, so it is already clean (no progress noise to filter)."""
    if not stdout:
        return False
    write_output(f"{stdout}\n")
    return True


def download_from_s3(
    cfg: Config,
    entry: str,
    outpath: str,
    is_dir: bool,
    verbose: bool,
    sub: str | None = None,
    compare: PairFilter | None = None,
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
                rel, outpath, compare=compare, dryrun=dryrun, verbose=verbose
            )
        finally:
            for path in created:  # leaf-first, so each rmdir empties its parent
                try:
                    os.rmdir(path)
                except OSError:
                    break
        if result.returncode != 0:
            if result.stderr:
                write_stderr(result.stderr)
            return result.returncode, False
        return 0, _print_transfer_lines(result.stdout)

    # Single file: a transfer always happens (we only reach here on a manifest
    # mismatch), so a successful download counts as changed - which keeps the
    # dry-run stand-in line ("would apply manifest metadata") printed for it.
    # `size` (from the manifest record) routes a
    # large file through multipart download; a small one is a direct GetObject.
    if dryrun:
        # The download writes the local file, so it is a mutation and stays
        # skipped. No substitute probe either: a HeadObject can succeed or
        # fail under different IAM permissions than the real GetObject, and a
        # dry run must only make the calls the real run would make.
        write_output(f"(dry-run) download: {cfg.prefix}/{rel} -> {outpath}\n")
        return 0, True
    if not cfg.store.get_object(rel, outpath, size=size, verbose=verbose):
        err(f"object missing on S3: {cfg.prefix}/{rel}")
        return 1, False
    return 0, True
