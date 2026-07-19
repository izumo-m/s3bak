# Requires Python 3.10+
"""The command layer: one ``cmd_*`` per subcommand plus their private helpers.

Orchestrates the lower layers - store (S3), syncops (manifest<->S3),
restore (local filesystem), compare (status/diff) - into the push / pull /
status / diff / show / list / ls-remote / verify behaviours. ``cli.py`` parses
argv and dispatches here.
"""

from __future__ import annotations

import os
import shutil
import stat as stat_mod
import subprocess
import tempfile
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from s3bak import localwalk, manifest
from s3bak.compare import (
    _diff_color_flag,
    _fmt_mtime,
    _resolve_use_color,
    check_metadata,
    compare_to_local,
    compare_to_stat,
    format_diff_block,
    mode_differs,
)
from s3bak.config import Config, Opts
from s3bak.confirm import (
    AnswerMode,
    DeleteConfirmer,
    DeletionAbortedError,
    confirm_subtree_delete,
    resolve_answer_mode,
)
from s3bak.console import (
    IS_WINDOWS,
    echo_command,
    err,
    normalize_local_path,
    note_warning,
    write_output,
    write_stderr,
)
from s3bak.manifest import ManifestEntry
from s3bak.restore import (
    apply_manifest,
    local_keyed,
    manifest_keyed,
    manifest_target,
    prepare_dir_conflicts,
    remove_extras,
    resolve_manifest_rel,
    resolve_pull_destination,
    windows_collect_writable_prep,
    windows_restore_modes,
)
from s3bak.store import ObjectMeta
from s3bak.syncops import (
    PushJournal,
    download_from_s3,
    download_manifest,
    drop_subtree_records,
    patch_manifest_subtree,
    publish_journal_manifest,
    sync_compare,
    write_manifest_to_aws,
)

if TYPE_CHECKING:
    from boto3_s3 import FileFilter, FileInfo


def _run_hook(name: str, hook: list[str] | None, opts: Opts) -> int:
    """Run one configured hook directly, without a command shell. A failing
    hook's status propagates (the documented 3+ lane), normalized where it
    would collide with s3bak's own exit codes: 2 is reserved for a
    warnings-only run, so it maps to 1, and a signal death (negative
    returncode) becomes the conventional 128+N instead of leaking a negative
    value into sys.exit."""
    if not hook:
        return 0
    if opts.dryrun:
        print(f"(dry-run) would run {name}: {hook!r}")
        return 0
    if opts.verbose:
        write_stderr(f"+ {name}: {hook!r}\n")
    rc = subprocess.run(hook, shell=False).returncode
    if rc == 0:
        return 0
    err(f"{name} failed (exit {rc}): {hook!r}")
    if rc < 0:
        return 128 - rc  # killed by signal N -> 128+N
    return 1 if rc == 2 else rc


def upload_manifest(
    cfg: Config,
    entry: str,
    target: str,
    excludes: list[str],
    opts: Opts,
    *,
    old_manifest: str | None = None,
    keep_old: bool = False,
) -> int:
    """Write the manifest from a fresh walk (the ``--meta-only`` rewrite, and
    the single-file entry's one-record write), then run the entry's
    post_hook. An ordinary directory push publishes its journal instead."""
    post_hook: list[str] | None = cfg.entries[entry].get("post_hook")

    if opts.dryrun:
        print(f"(dry-run) would update manifest: {manifest.manifest_key(entry)}")
        # The walk and merge write only a local temp file: run them so the
        # rehearsal emits the same structural warnings as the real push,
        # skipping only the upload.
        write_manifest_to_aws(
            cfg,
            entry,
            target,
            excludes,
            opts.verbose,
            old_manifest=old_manifest,
            keep_old=keep_old,
            upload=False,
        )
        return _run_hook("post_hook", post_hook, opts)

    write_manifest_to_aws(
        cfg, entry, target, excludes, opts.verbose, old_manifest=old_manifest, keep_old=keep_old
    )

    return _run_hook("post_hook", post_hook, opts)


@dataclass
class _PushDeletePlan:
    """How this push treats S3 orphans: the sync's delete-lane value. What
    each answer means for the manifest is the journal emitter's business (a
    confirmed deletion journals its record's drop at the decision point)."""

    lane: bool | FileFilter  # sync_up delete=: False | True | per-orphan callable
    confirmer: DeleteConfirmer | None  # --delete without --yes (asked or auto-n)
    mirror: bool  # --delete --yes: every record follows its object
    walker: localwalk.ManifestWalker | None = None  # the sync's local walker (--delete only)
    old_manifest: str | None = None  # set by the caller once downloaded
    refused: int = 0  # candidates refused because the local scan was incomplete
    _files: manifest.RecordedFiles | None = None

    def _scan_incomplete(self) -> bool:
        return self.walker is not None and self.walker.scan_incomplete

    def allow(self) -> bool:
        """The delete lane's completeness gate, checked per candidate: once
        the local walk has warn-skipped real content (an unreadable directory
        or file, a path that vanished mid-walk), every later candidate is
        refused - an orphan decision built on a partial local view could
        delete a good backup. Decisions made before the gap were sound and
        stand."""
        if self._scan_incomplete():
            self.refused += 1
            return False
        return True

    def recorded(self, rel: str) -> bool:
        """Whether the old manifest holds a regular-file record for this delete
        candidate. Lazy: the manifest is downloaded after the plan is built, so
        the stream opens on the first candidate, once ``old_manifest`` is set."""
        if self._files is None:
            self._files = manifest.RecordedFiles(self.old_manifest)
        return self._files.contains(rel)

    def close(self) -> None:
        if self._files is not None:
            self._files.close()


def _plan_push_deletes(
    cfg: Config, entry: str, sub: str | None, opts: Opts, walker: localwalk.ManifestWalker
) -> _PushDeletePlan:
    if not opts.delete:
        return _PushDeletePlan(lane=False, confirmer=None, mirror=False)
    if opts.dryrun or resolve_answer_mode(yes=opts.yes) is AnswerMode.ALL_YES:
        # Report (dry run) or delete (--yes) every candidate the completeness
        # gate admits. The gate callable never prompts, so it is safe under
        # dryrun too (the library invokes a callable there as well).
        plan = _PushDeletePlan(lane=True, confirmer=None, mirror=opts.yes, walker=walker)
        plan.lane = lambda info: plan.allow()
        return plan
    # ASK and ALL_NO both run the lane through a confirmer. ALL_NO never
    # prompts, but its auto-n answers still record every existing orphan -
    # which is what lets the merge keep exactly those file records and drop
    # stale ones (records whose object is already gone).
    confirmer = DeleteConfirmer(resolve_answer_mode(yes=opts.yes), entry)

    def decide(info: FileInfo) -> bool:
        if not plan.allow():
            return False
        # compare_key is relative to the sync's S3 listing prefix, i.e. to the
        # sub on a sub-path push; the prompt shows the entry-rooted key.
        assert info.compare_key is not None  # the sync stamps every listed entry
        rel = info.compare_key if sub is None else f"{sub}/{info.compare_key}"
        # A candidate the manifest never recorded is outside the backup: n
        # keeps its object for this run only, so the prompt says so.
        note = "" if plan.recorded(rel) else " (not in manifest)"
        return confirmer.confirm(f"{cfg.prefix}/{entry}/{rel}{note}")

    plan = _PushDeletePlan(lane=decide, confirmer=confirmer, mirror=False, walker=walker)
    return plan


def _warn_refused_deletes(entry: str, plan: _PushDeletePlan) -> None:
    """After the sync: say why deletion candidates were kept when the local
    scan was incomplete (see _PushDeletePlan.allow)."""
    if plan.refused:
        note_warning(
            f"warning: {entry}: the local scan skipped unreadable or vanished paths;"
            f" kept {plan.refused} deletion candidate(s) and every manifest record"
        )


def _warn_unrecorded_uploads(entry: str, opts: Opts, journal: PushJournal) -> None:
    """Emit the --data-only unrecorded-upload warning after a successful sync.
    The journal tallies uploads with no owning file record at decision time
    (create and update lanes both - the birth and the re-upload faces of an
    unrecorded object), so the warning repeats on every push while the object
    stays unrecorded. A dry run makes the same decisions without
    transferring, so it previews the warning (and its exit 2) with "would
    upload" wording."""
    if opts.data_only and journal.unrecorded_uploads:
        verb = "would upload" if opts.dryrun else "uploaded"
        note_warning(
            f"warning: {entry}: --data-only {verb} {journal.unrecorded_uploads} object(s)"
            f" the manifest does not record; run a push without --data-only to record them"
        )


def _delete_conflict_objects(
    cfg: Config, entry: str, plan: _PushDeletePlan, journal: PushJournal, opts: Opts
) -> tuple[int, bool]:
    """Offer the kind-conflict objects a --delete run collected (see
    PushJournal.pending_object_deletes): a local symlink or special file
    occupies a key holding a real S3 object, so the object formed an update
    pair and the sync's delete lane never saw it. Confirmed like any other
    candidate - through the same confirmer, so a/d stickiness carries over -
    and deleted through the store directly. The record at the key describes
    the local non-file (already journaled) and is untouched: only the object
    goes. Returns ``(status, deleted_any)``."""
    doomed: list[str] = []
    for rel, recorded in journal.pending_object_deletes:
        if not plan.allow():
            continue
        if plan.confirmer is not None:
            note = "" if recorded else " (not in manifest)"
            if not plan.confirmer.confirm(f"{cfg.prefix}/{entry}/{rel}{note}"):
                continue
        doomed.append(f"{entry}/{rel}")
    if not doomed:
        return 0, False
    assert cfg.store is not None
    result = cfg.store.delete_objects(doomed, dryrun=opts.dryrun, verbose=opts.verbose)
    if result.stdout:
        write_output(f"{result.stdout}\n")
    if result.stderr:
        write_stderr(f"{result.stderr}\n")
    return result.returncode, result.returncode == 0


def _push_sub(
    cfg: Config,
    entry: str,
    post_hook: list[str] | None,
    target_root: str,
    sub: str,
    excludes: list[str],
    opts: Opts,
) -> int:
    local_sub = os.path.join(target_root, sub)
    sub_rel = f"{entry}/{sub}"
    s3_sub_path = f"{cfg.prefix}/{sub_rel}"
    assert cfg.store is not None

    walker = localwalk.sync_walker(excludes, sub=sub)
    plan = _plan_push_deletes(cfg, entry, sub, opts, walker)
    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        # Download and validate the manifest before ANY S3 mutation - the
        # deletion, upload, and patch below all reuse this one validated copy,
        # so a damaged manifest aborts the push while the backup is still
        # intact. The download is read-only and runs under --dry-run too.
        have_manifest = download_manifest(cfg, entry, manifest_path, opts.verbose)
        old_manifest = manifest_path if have_manifest else None
        if have_manifest and _entry_kind_from_manifest(manifest_path) == "file":
            # Patching a sub-path into a file-shaped manifest would corrupt it;
            # push the entry itself to migrate the kind first.
            err(f"sub path not allowed for single-file entry: {entry}")
            return 1
        if have_manifest:
            plan.old_manifest = manifest_path

        if not os.path.lexists(local_sub):
            if not opts.delete:
                err(f"local path does not exist (use --delete to remove its backup): {local_sub}")
                return 1
            # --delete cannot combine with --meta-only/--data-only (rejected at
            # the CLI), so this is always the full deletion: one confirmation
            # covers the subtree's objects and manifest records together.
            if not opts.dryrun and not confirm_subtree_delete(
                resolve_answer_mode(yes=opts.yes), entry, s3_sub_path
            ):
                err(f"backup subtree not deleted (answer y, or use --yes): {s3_sub_path}")
                return 1
            result = cfg.store.delete_subtree(sub_rel, dryrun=opts.dryrun, verbose=opts.verbose)
            if result.stdout:
                write_output(f"{result.stdout}\n")
            if result.returncode != 0:
                if result.stderr:
                    write_stderr(f"{result.stderr}\n")
                return result.returncode
            did_work = bool(result.stdout)
            did_work = drop_subtree_records(cfg, entry, old_manifest, sub, opts) or did_work
            return _run_hook("post_hook", post_hook, opts) if did_work else 0

        if opts.meta_only:
            # --meta-only --delete is rejected at the CLI: always the keep policy.
            did_work = patch_manifest_subtree(
                cfg,
                entry,
                target_root,
                sub,
                excludes,
                opts,
                keep_old=True,
                old_manifest=old_manifest,
            )
            return _run_hook("post_hook", post_hook, opts) if did_work else 0

        st = os.lstat(local_sub)
        is_link = stat_mod.S_ISLNK(st.st_mode)
        is_dir_sub = not is_link and os.path.isdir(local_sub)
        if not (is_link or is_dir_sub or stat_mod.S_ISREG(st.st_mode)):
            err(f"sub path must be a regular file, directory, or symlink: {local_sub}")
            return 1

        did_work = False
        conflict_deleted = False
        journal_fd, journal_path = tempfile.mkstemp(suffix=".journal")
        os.close(journal_fd)
        try:
            # A non-directory sub-path has no S3 listing, so --delete has no
            # candidates to confirm there: old records under a same-named
            # former directory are kept (with the restorability warning);
            # pruning them takes a directory-level push --delete. Hence
            # delete_mode only for the directory branch.
            journal = PushJournal(
                journal_path,
                old_manifest,
                window_ns=cfg.window_ns_for(entry),
                walker=walker,
                sub=sub,
                content=cfg.store.content_compare() if opts.checksum else None,
                delete_mode=opts.delete and is_dir_sub,
                mirror=opts.delete and opts.yes and is_dir_sub,
            )
            try:
                if old_manifest is None:
                    # First-ever manifest born from a sub-path push: record the
                    # entry root so the manifest keeps its dir-entry shape and
                    # the root's metadata restores on pull.
                    journal.record_root(os.lstat(target_root))
                # Ancestor records for sub's parents: every record needs a
                # recorded directory parent (the validator's rule); only a
                # missing or drifted ancestor journals.
                acc = target_root
                rel_acc: str | None = None
                for part in sub.split("/")[:-1]:
                    acc = os.path.join(acc, part)
                    rel_acc = part if rel_acc is None else f"{rel_acc}/{part}"
                    journal.record_ancestor(rel_acc, os.lstat(acc))

                if is_link:
                    # symlink: upload nothing; the manifest record IS the backup.
                    journal.record_target(sub, st, os.readlink(local_sub))
                elif is_dir_sub:
                    delete_lane: bool | FileFilter = (
                        journal.observe_delete(plan.lane) if callable(plan.lane) else plan.lane
                    )
                    result = cfg.store.sync_up(
                        local_sub,
                        sub_rel,
                        walker=walker,
                        compare=journal.update_filter,
                        create=journal.create_filter,
                        delete=delete_lane,
                        dryrun=opts.dryrun,
                        verbose=opts.verbose,
                    )
                    if result.returncode != 0:
                        write_output(result.stdout)
                        if result.stderr:
                            write_stderr(result.stderr)
                        return result.returncode
                    if result.stdout:
                        write_output(f"{result.stdout}\n")
                        did_work = True
                    _warn_unrecorded_uploads(entry, opts, journal)
                else:
                    # Regular file: an explicit sub-path push always uploads,
                    # and always re-records - naming the path is the
                    # instruction to back up its current state.
                    if opts.dryrun:
                        print(f"(dry-run) upload: {local_sub} -> {s3_sub_path}")
                        did_work = True
                    else:
                        result = cfg.store.put_object(sub_rel, local_sub, verbose=opts.verbose)
                        if result.returncode != 0:
                            write_output(result.stdout)
                            if result.stderr:
                                write_stderr(result.stderr)
                            return result.returncode
                        if result.stdout:
                            write_output(f"{result.stdout}\n")
                            did_work = True
                    journal.record_target(sub, st, None)
            finally:
                journal.close()
            if journal.pending_object_deletes:
                st_del, conflict_deleted = _delete_conflict_objects(cfg, entry, plan, journal, opts)
                if st_del != 0:
                    return st_del
            # After the conflict candidates: their refusals (incomplete scan)
            # must count in the summary too.
            _warn_refused_deletes(entry, plan)
            if journal.has_events and not opts.data_only:
                publish_journal_manifest(cfg, entry, old_manifest, journal_path, opts)
                did_work = True
        finally:
            os.unlink(journal_path)
        if conflict_deleted:
            did_work = True
        return _run_hook("post_hook", post_hook, opts) if did_work else 0
    finally:
        plan.close()
        os.unlink(manifest_path)


def _single_file_compare(
    cfg: Config, entry: str, target: str, opts: Opts, manifest_path: str | None
) -> tuple[bool, bool]:
    """The single-file counterpart of the sync compare: size+mtime check against
    the entry's one-record manifest (or EtagComparison under --checksum).
    ``manifest_path`` is the caller's already-downloaded manifest (file-shaped -
    the kind guard ran), or None when the entry has none on S3.
    Returns ``(needs_upload, mode_drifted)``.

    Upload unless the manifest holds a regular-file record for exactly this
    basename, the local stat matches it, AND the S3 object exists at the
    recorded size - a `--meta-only` push or an S3-side delete leaves a
    manifest with no object behind it, and an out-of-band overwrite leaves
    one at the wrong size; only this head-object probe can see either (a dir
    entry self-heals via the sync listing; a single file has no listing).

    ``mode_drifted`` reports a permission-only drift against that record when
    no upload is needed, so the caller refreshes just the manifest - the same
    record is already in hand, costing no extra S3 call. The --checksum path
    never sets it (its ETag decision reads no manifest here);
    _single_file_manifest_matches covers mode there."""
    assert cfg.store is not None
    if opts.checksum:
        return cfg.store.needs_upload(entry, target, verbose=opts.verbose), False
    if manifest_path is None:
        return True, False
    st = os.lstat(target)
    basename = os.path.basename(target)
    for m in manifest.iter_manifest(manifest_path):
        if m.path == basename and m.sym_target is None and m.is_file:
            if not m.matches_stat(st, cfg.window_ns_for(entry)):
                return True, False
            head = cfg.store.head_object(entry, verbose=opts.verbose)
            if head is None or head.size != m.size:
                return True, False
            return False, mode_differs(m, st)
    return True, False


def _single_file_manifest_matches(manifest_path: str, target: str) -> bool:
    """Whether the already-downloaded manifest describes this single-file
    entry: the record names the configured basename and its permission bits
    match the local file."""
    record = next(manifest.iter_manifest(manifest_path))
    if record.path != os.path.basename(target):
        return False
    return not mode_differs(record, os.lstat(target))


def _migrate_entry_kind(cfg: Config, entry: str, to_dir: bool, opts: Opts) -> int:
    """The entry's local path changed kind (file <-> directory) since the last
    push. The old backup cannot merge into the new one: a bare-basename record
    is invalid inside a directory manifest, and a directory tree's objects
    would silently orphan under a single-file manifest. So the old backup -
    the exact key and everything under ``entry/`` - is deleted first, behind
    the same one-question confirmation as a subtree deletion, and the push
    then records the new kind from scratch. Without ``--delete`` the push
    refuses, so a surprise type change cannot erase a backup. Interruptions
    self-heal: the manifest still records the old kind, so the next push
    lands back here."""
    assert cfg.store is not None
    old_kind, new_kind = ("file", "directory") if to_dir else ("directory", "file")
    if not opts.delete:
        err(
            f"{entry}: the backup records a {old_kind} but the local path is now a"
            f" {new_kind}; push --delete replaces the old backup"
        )
        return 1
    display = f"{cfg.prefix}/{entry}"
    if not opts.dryrun and not confirm_subtree_delete(
        resolve_answer_mode(yes=opts.yes), entry, display
    ):
        err(f"old backup not deleted (answer y, or use --yes): {display}")
        return 1
    result = cfg.store.delete_subtree(entry, dryrun=opts.dryrun, verbose=opts.verbose)
    if result.stdout:
        write_output(f"{result.stdout}\n")
    if result.returncode != 0:
        if result.stderr:
            write_stderr(f"{result.stderr}\n")
        return result.returncode
    return 0


def _delete_file_entry_strays(cfg: Config, entry: str, opts: Opts) -> tuple[int, str]:
    """``push --delete`` for a single-file entry: offer the objects under
    ``entry/`` for deletion. A file-shaped manifest records only the entry's
    own key, so anything below ``entry/`` is outside the backup - the residue
    of an entry that used to be a directory, or an out-of-band upload - and
    would otherwise be invisible to every command but verify. The same
    per-object confirmation as the directory delete lane; the manifest is not
    touched (these keys have no records). Returns ``(status, output_lines)``."""
    assert cfg.store is not None
    candidates = (f"{entry}/{o.key}" for o in cfg.store.iter_objects(entry, verbose=opts.verbose))
    if opts.dryrun or resolve_answer_mode(yes=opts.yes) is AnswerMode.ALL_YES:
        result = cfg.store.delete_objects(candidates, dryrun=opts.dryrun, verbose=opts.verbose)
    else:
        mode = resolve_answer_mode(yes=opts.yes)
        if mode is AnswerMode.ALL_NO:
            # Every answer is no: keep everything (each object returns as a
            # candidate on the next --delete).
            return 0, ""
        doomed: list[str] = []
        confirmer = DeleteConfirmer(mode, entry)
        for rel in candidates:
            if confirmer.confirm(f"{cfg.prefix}/{rel} (not in manifest)"):
                doomed.append(rel)
        if not doomed:
            return 0, ""
        result = cfg.store.delete_objects(doomed, verbose=opts.verbose)
    if result.stderr:
        write_stderr(f"{result.stderr}\n")
    return result.returncode, result.stdout


def cmd_push(cfg: Config, entry: str, opts: Opts, sub: str | None = None) -> int:
    entry_cfg = cfg.entries.get(entry)
    if not entry_cfg:
        err(f"no such entry: {entry}")
        return 1
    target: str = entry_cfg["path"]
    target_root = normalize_local_path(target)

    excludes: list[str] = entry_cfg.get("excludes", [])

    # Hook contract: pre_hook runs before every push attempt. post_hook is
    # deliberately asymmetric - it runs only after a push that did work, i.e.
    # that transferred data and/or refreshed the manifest (see upload_manifest,
    # the data-only branch below, and _push_sub), or whenever --meta-only is
    # given (which always refreshes the manifest and runs the hook). A pure
    # no-op push runs no post_hook on purpose, so side-effecting hooks (e.g.
    # rclone) do not fire when nothing changed; use --meta-only to run the hook
    # on demand. By design, not a bug.
    pre_hook: list[str] | None = entry_cfg.get("pre_hook")
    if pre_hook:
        st = _run_hook("pre_hook", pre_hook, opts)
        if st != 0:
            return st

    # Validate after pre_hook: hooks commonly generate the file/tree being
    # backed up, and the documented pipeline promises they run first. A missing
    # root is allowed only for an explicit sub-path deletion, which needs no
    # local data source when an existing manifest can be patched.
    if not os.path.lexists(target_root):
        if sub is None or not opts.delete:
            err(f"target does not exist: {target}")
            return 1
    else:
        mode = os.lstat(target_root).st_mode
        if stat_mod.S_ISLNK(mode):
            err(f"entry path is a symlink, which is not allowed as an entry: {target}")
            return 1
        if not (stat_mod.S_ISREG(mode) or stat_mod.S_ISDIR(mode)):
            err(f"entry path must be a regular file or directory: {target}")
            return 1

    if sub is not None:
        if os.path.lexists(target_root) and not os.path.isdir(target_root):
            err(f"sub path not allowed for single-file entry: {entry}")
            return 1
        post_hook_sub: list[str] | None = entry_cfg.get("post_hook")
        try:
            return _push_sub(cfg, entry, post_hook_sub, target_root, sub, excludes, opts)
        except DeletionAbortedError:
            err(f"{entry}: aborted")
            return 1

    results = ""
    refresh_manifest = False
    assert cfg.store is not None

    walker = localwalk.sync_walker(excludes)
    plan = _plan_push_deletes(cfg, entry, None, opts, walker)
    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        # Every push - every mode - downloads and validates the manifest
        # first: an ordinary push compares against it, any push uses it to
        # notice objectless tree changes or an entry kind change, --data-only
        # reads it to warn about the uploads it leaves unrecorded, and a
        # damaged manifest must abort the push before anything on S3 moves.
        # All of that is read-only, so it runs under --dry-run too - a
        # rehearsal surfaces problems here.
        have_manifest = download_manifest(cfg, entry, manifest_path, opts.verbose)
        is_dir_target = os.path.isdir(target)
        if have_manifest and (_entry_kind_from_manifest(manifest_path) == "dir") != is_dir_target:
            if opts.meta_only:
                # --meta-only moves no data, so it cannot migrate a kind
                # change; recording the new kind anyway would silently orphan
                # the old tree or corrupt the manifest.
                err(
                    f"{entry}: the backup and the local path disagree on kind"
                    f" (file vs directory); push --delete migrates it"
                )
                return 1
            st = _migrate_entry_kind(cfg, entry, is_dir_target, opts)
            if st != 0:
                return st
            have_manifest = False  # the old backup is gone: record from scratch
        if have_manifest:
            plan.old_manifest = manifest_path

        # --meta-only refreshes the manifest and runs the post_hook even with
        # no data change: the supported way to re-run the post_hook on demand
        # (intended). A directory refresh merges against the old manifest with
        # every old-only record kept: --meta-only moves no data, so it must
        # not drop the records of objects that are still on S3
        # (--meta-only --delete is rejected).
        if opts.meta_only:
            if not is_dir_target:
                return upload_manifest(cfg, entry, target, excludes, opts)
            return upload_manifest(
                cfg,
                entry,
                target,
                excludes,
                opts,
                old_manifest=manifest_path if have_manifest else None,
                keep_old=True,
            )

        if is_dir_target:
            journal_fd, journal_path = tempfile.mkstemp(suffix=".journal")
            os.close(journal_fd)
            conflict_deleted = False
            try:
                journal = PushJournal(
                    journal_path,
                    manifest_path if have_manifest else None,
                    window_ns=cfg.window_ns_for(entry),
                    walker=walker,
                    content=cfg.store.content_compare() if opts.checksum else None,
                    delete_mode=opts.delete,
                    mirror=opts.delete and opts.yes,
                )
                try:
                    delete_lane: bool | FileFilter = (
                        journal.observe_delete(plan.lane) if callable(plan.lane) else plan.lane
                    )
                    result = cfg.store.sync_up(
                        target,
                        entry,
                        walker=walker,
                        compare=journal.update_filter,
                        create=journal.create_filter,
                        delete=delete_lane,
                        dryrun=opts.dryrun,
                        verbose=opts.verbose,
                    )
                finally:
                    # Flush the journal (and release the old-manifest handle the
                    # cursor holds open - an open file cannot be removed on
                    # Windows) whether or not the sync succeeded.
                    journal.close()
                if result.returncode != 0:
                    write_output(result.stdout)
                    if result.stderr:
                        write_stderr(result.stderr)
                    return result.returncode
                results = result.stdout
                if results:
                    write_output(f"{results}\n")
                _warn_unrecorded_uploads(entry, opts, journal)
                if journal.pending_object_deletes:
                    st_del, conflict_deleted = _delete_conflict_objects(
                        cfg, entry, plan, journal, opts
                    )
                    if st_del != 0:
                        return st_del
                # After the conflict candidates: their refusals (incomplete
                # scan) must count in the summary too.
                _warn_refused_deletes(entry, plan)
                # The rewrite condition is "the journal is non-empty", nothing
                # else: a first push journals everything (the root included),
                # a pure no-op push journals nothing.
                if journal.has_events and not opts.data_only:
                    publish_journal_manifest(
                        cfg,
                        entry,
                        manifest_path if have_manifest else None,
                        journal_path,
                        opts,
                    )
                    refresh_manifest = True
                else:
                    refresh_manifest = False
            finally:
                os.unlink(journal_path)
            if results or refresh_manifest or conflict_deleted:
                post_hook: list[str] | None = entry_cfg.get("post_hook")
                return _run_hook("post_hook", post_hook, opts)
            return 0
        else:
            needs_upload, mode_drifted = _single_file_compare(
                cfg, entry, target, opts, manifest_path if have_manifest else None
            )
            if needs_upload:
                # Single-file entry that fails the size+mtime check against its
                # manifest (or the --checksum ETag comparison), or was never
                # pushed: upload it.
                if opts.dryrun:
                    # Set results only; the shared writer below emits it (and the
                    # truthy results drives the dryrun manifest line). Printing
                    # here too would double the line.
                    results = f"(dry-run) upload: {target} -> {cfg.prefix}/{entry}"
                else:
                    result = cfg.store.put_object(entry, target, verbose=opts.verbose)
                    if result.returncode != 0:
                        write_output(result.stdout)
                        if result.stderr:
                            write_stderr(result.stderr)
                        return result.returncode
                    results = result.stdout
                refresh_manifest = bool(results)
            elif not opts.data_only:
                if opts.checksum:
                    # ETag equality can skip an already-present data object even
                    # when its manifest was deleted, still names an older
                    # configured basename, or records a stale mode.
                    refresh_manifest = not have_manifest or not _single_file_manifest_matches(
                        manifest_path, target
                    )
                else:
                    refresh_manifest = mode_drifted
            if opts.delete:
                # A single-file entry has no sync listing, so its --delete lane
                # is this explicit sweep of entry/ (see _delete_file_entry_strays).
                st, stray_lines = _delete_file_entry_strays(cfg, entry, opts)
                if stray_lines:
                    write_output(f"{stray_lines}\n")
                    # Deletions are work: refresh the manifest (a no-op rewrite
                    # of the single record) so post_hook fires, as a directory
                    # delete-only push would.
                    refresh_manifest = True
                if st != 0:
                    return st

        if results:
            write_output(f"{results}\n")

        # Single-file refresh: after an upload, a mode drift, or a stray
        # deletion (a no-op rewrite of the one record, so post_hook fires as
        # a directory delete-only push would). An mtime drift inside the
        # window does not refresh an existing manifest (the window is a
        # rounding tolerance).
        if refresh_manifest and not opts.data_only:
            st = upload_manifest(cfg, entry, target, excludes, opts)
            if st != 0:
                return st

        if results and opts.data_only:
            post_hook_file: list[str] | None = entry_cfg.get("post_hook")
            return _run_hook("post_hook", post_hook_file, opts)

        return 0
    except DeletionAbortedError:
        # q mid-confirmation: already-confirmed deletions may have run, but the
        # manifest was not rewritten and no hook fires. The next push --delete
        # settles any records those deletions left behind.
        err(f"{entry}: aborted")
        return 1
    finally:
        plan.close()
        os.unlink(manifest_path)


def _entry_kind_from_manifest(manifest_path: str) -> str:
    """Return ``"dir"`` or ``"file"`` for an already validated manifest."""
    first = next(manifest.iter_manifest(manifest_path), None)
    if first is None:  # Defensive for direct callers; downloads validate first.
        raise manifest.ManifestError("manifest contains no records")
    return "dir" if first.path == "." else "file"


def _sub_kind_from_manifest(manifest_path: str, sub: str) -> str:
    """Return file, dir, symlink, special, or missing for a manifest sub-path.

    A descendant under sub proves it is a directory; otherwise the recorded
    type decides (an empty directory has no descendants and no S3 object, but
    its type is recorded). Symlinks and special files have no data object;
    apply_manifest recreates a symlink, while a special file must already exist
    locally for metadata to be applied."""
    self_entry: ManifestEntry | None = None
    for entry in manifest.iter_manifest(manifest_path):
        rel = entry.path.removeprefix("./")
        if rel == sub:
            self_entry = entry
        elif rel.startswith(sub + "/"):
            return "dir"
    if self_entry is None:
        return "missing"
    if self_entry.is_dir:
        return "dir"
    if self_entry.is_file:
        return "file"
    return "symlink" if self_entry.sym_target is not None else "special"


def _manifest_matches_local(
    manifest_path: str, outpath: str, is_dir: bool, sub: str | None, window_ns: int
) -> bool:
    """True iff every manifest record matches the local filesystem.

    Returning True means 'boto3-s3 sync' would copy nothing AND apply_manifest
    would change nothing - so both can be skipped.
    """
    for entry in manifest.iter_manifest(manifest_path):
        res = manifest_target(entry, outpath, is_dir, sub)
        if res is None:
            continue
        target, _rel = res
        if not compare_to_local(entry, target, window_ns=window_ns).is_match:
            return False
    return True


def cmd_pull(cfg: Config, entry: str, opts: Opts, sub: str | None = None) -> int:
    entry_cfg = cfg.entries.get(entry)
    configured_path: str | None = entry_cfg["path"] if entry_cfg else None
    outpath = resolve_pull_destination(entry, configured_path, sub, opts.outpath)
    if outpath is None:
        err(f"no such entry in config: {entry}")
        err("use -o <path> to specify the output path")
        return 1

    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        # 1. Fetch the manifest first; its content tells us file/dir
        #    without any extra head-object calls.
        if not download_manifest(cfg, entry, manifest_path, opts.verbose):
            if sub is not None:
                err(f"not found on S3: {entry}/{sub}")
            else:
                err(f"entry not found on S3: {entry}")
            return 1

        entry_is_dir = _entry_kind_from_manifest(manifest_path) == "dir"

        if sub is not None:
            if not entry_is_dir:
                err(f"sub path not allowed for single-file entry: {entry}")
                return 1
            kind = _sub_kind_from_manifest(manifest_path, sub)
            if kind == "missing":
                err(f"not found on S3: {entry}/{sub}")
                return 1
            is_dir = kind == "dir"
            has_data = kind in ("file", "dir")
        else:
            is_dir = entry_is_dir
            has_data = True

        # 2. If everything in the manifest already matches local, both
        #    the s3 sync/cp and apply_manifest are no-ops. Skip them. Not
        #    under --checksum: this gate is the same size+mtime check whose
        #    blind spot --checksum exists to cover, so it must not stand
        #    between the user and the content comparison.
        window_ns = cfg.window_ns_for(entry)
        excludes: list[str] = entry_cfg.get("excludes", []) if entry_cfg else []
        manifest_matches = _manifest_matches_local(manifest_path, outpath, is_dir, sub, window_ns)
        if manifest_matches and not opts.checksum:
            if not opts.meta_only and opts.delete and is_dir:
                return _mirror_extras(
                    manifest_path,
                    outpath,
                    sub,
                    excludes,
                    opts=opts,
                    entry=entry,
                    window_ns=window_ns,
                )
            return 0

        # Make the restore root's type agree before any transfer. In particular,
        # never let a directory sync walk through a symlink root, and never let
        # a single-file write follow one. s3transfer/direct downloads replace
        # inner leaves atomically; this handles the operation root itself.
        # A conflicting root is not destroyed up front: the download lands in a
        # unique stage directory beside it (mkdtemp - a fixed name could
        # collide with unrelated user data) and the root is swapped only after
        # it succeeded, so a failed download cannot cost the local state it
        # was replacing. A dry run reports the conflict instead of staging;
        # the dry-run sync then runs against the uncorrected root, so its
        # transfer report may differ from what the real pull does.
        stage_dir: str | None = None
        stage_holds_old_root = False
        prep: list[tuple[str, int]] = []
        if has_data and not opts.meta_only and os.path.lexists(outpath):
            if is_dir:
                conflict = os.path.islink(outpath) or not os.path.isdir(outpath)
            else:
                conflict = not stat_mod.S_ISREG(os.lstat(outpath).st_mode)
            if conflict:
                if opts.dryrun:
                    write_output(f"(dry-run) would replace {outpath} (conflicting type)\n")
                else:
                    parent = os.path.dirname(os.path.abspath(outpath))
                    stage_dir = tempfile.mkdtemp(
                        prefix=os.path.basename(outpath) + ".s3bak-stage-", dir=parent
                    )

        try:
            # Replace local symlinks sitting at recorded directory paths before
            # the sync: it opens dir/file paths through whatever is at dir, so
            # a symlink there would route downloads outside the restore tree.
            # (A fresh stage has no such symlinks, and the metadata apply
            # settles every other conflicting type after the download.)
            if (
                is_dir
                and has_data
                and not opts.meta_only
                and not opts.dryrun
                and stage_dir is None
                and os.path.isdir(outpath)
                and prepare_dir_conflicts(outpath, manifest_path, sub)
            ):
                return 1

            # 3. Normal path: prep, then sync (dir) or cp (file). Root correction
            # must precede the Windows writable pass so that pass cannot traverse a
            # symlinked restore root and chmod a file outside the destination. A
            # staged pull downloads into a fresh stage, where nothing needs prep
            # (and the conflicting root itself must not be walked into).
            if IS_WINDOWS and not opts.meta_only and not opts.dryrun and stage_dir is None:
                prep = windows_collect_writable_prep(outpath, is_dir, manifest_path, sub)

            changed = False
            if not opts.meta_only and has_data:
                # The compare only matters for the dir sync; a single-file transfer
                # always happens (we only reach it on a manifest mismatch). Its
                # size (from the manifest) routes a large file through multipart.
                dest = os.path.join(stage_dir, "new") if stage_dir is not None else outpath
                compare = sync_compare(cfg, opts, entry, manifest_path, sub=sub) if is_dir else None
                file_size = None if is_dir else _single_file_size(manifest_path)
                try:
                    rc, changed = download_from_s3(
                        cfg,
                        entry,
                        dest,
                        is_dir,
                        opts.verbose,
                        sub=sub,
                        compare=compare,
                        size=file_size,
                        dryrun=opts.dryrun,
                    )
                finally:
                    # The streaming ManifestFilter holds the temp manifest open; close
                    # it before the outer finally unlinks it (Windows cannot remove an
                    # open file).
                    if isinstance(compare, manifest.ManifestFilter):
                        compare.close()
                if rc != 0:
                    if IS_WINDOWS:
                        windows_restore_modes(prep)
                    return rc
                if stage_dir is not None:
                    # The download is complete: swap in two atomic renames with
                    # the old root recoverable in between - the stage cleanup
                    # in the finally below then retires it (or, on a failed
                    # swap, the partial download).
                    replaced = os.path.join(stage_dir, "replaced")
                    os.replace(outpath, replaced)
                    try:
                        os.replace(dest, outpath)
                    except BaseException:
                        try:
                            os.replace(replaced, outpath)  # put the old root back
                        except OSError:
                            # The rollback itself failed: the cleanup below
                            # must not delete the only remaining copy.
                            stage_holds_old_root = True
                            err(f"could not restore {outpath}; it is preserved at {replaced}")
                        raise

            # 4. Apply manifest metadata (mode, mtime, symlinks): objectless or
            #    metadata-only diffs (empty dirs, symlinks, mode/mtime) have nothing
            #    to download yet still need applying. Only records whose local
            #    state differs from the record are touched. A downloaded file
            #    normally mismatches afterwards (the dir sync stamps the S3 upload
            #    time onto it, the file lane leaves the write time) and gets its
            #    recorded mtime back; a stamp landing inside the mtime window is a
            #    match and stays, like any other within-window drift. The gate also
            #    re-applies the recorded modes over the writable prep - no separate
            #    restore needed. Skipped with --data-only.
            if opts.data_only:
                if IS_WINDOWS and not opts.meta_only:
                    windows_restore_modes(prep)
                st = 0
            elif opts.dryrun:
                # One stand-in line for the metadata apply (mode / mtime /
                # symlinks), printed only when the real apply could repair
                # something: a stat-gate difference, or a planned transfer.
                if not manifest_matches or changed:
                    write_output(f"(dry-run) would apply manifest metadata: {outpath}\n")
                st = 0
            else:
                st = apply_manifest(
                    outpath, is_dir, manifest_path, sub=sub, window_ns=window_ns, excludes=excludes
                )

            if not opts.meta_only and opts.delete and is_dir:
                if st != 0:
                    # The local tree is not in the recorded state; extras built
                    # on that view are not trustworthy deletion candidates.
                    err(f"{entry}: skipping --delete (the metadata apply failed)")
                else:
                    st = _mirror_extras(
                        manifest_path,
                        outpath,
                        sub,
                        excludes,
                        opts=opts,
                        entry=entry,
                        window_ns=window_ns,
                    )

            return st
        except BaseException:
            # An exception (S3 error, local I/O, SIGINT) skips the normal
            # mode-restoring exits; put the Windows writable prep back before
            # it propagates.
            if IS_WINDOWS:
                windows_restore_modes(prep)
            raise
        finally:
            if stage_dir is not None and not stage_holds_old_root:
                # Retires the swapped-out old root on success, the partial
                # download on failure - never anything this pull did not make,
                # and never a stranded old root the rollback could not put back.
                shutil.rmtree(stage_dir, ignore_errors=True)
    except DeletionAbortedError:
        err(f"{entry}: aborted")
        return 1
    finally:
        os.unlink(manifest_path)


def _single_file_size(manifest_path: str) -> int | None:
    """Size of a single-file entry's sole data record (for the download size
    gate), or None if the manifest has no regular-file record."""
    for m in manifest.iter_manifest(manifest_path):
        if m.is_file and m.sym_target is None:
            return m.size
    return None


# The status / pull --delete diff runs on the manifest-vs-local-walk merge-join
# (restore.manifest_keyed / restore.local_keyed), the same streams the pull
# metadata apply consumes.


def _delete_extras(
    manifest_path: str,
    outpath: str,
    sub: str | None,
    excludes: list[str],
    *,
    opts: Opts,
    entry: str,
) -> tuple[int, int]:
    """Remove local paths the manifest does not record (pull ``--delete``),
    behind the per-item confirmation (--yes answers every question yes; a
    non-interactive run without --yes answers no, i.e. removes nothing).
    Returns ``(status, removals)``.

    The local-only lane of the merge-join; only the extras themselves are
    collected (never the whole key set) so the deepest-first removal order
    costs memory proportional to what is actually deleted."""
    confirmer: DeleteConfirmer | None = None
    if not opts.dryrun:
        mode = resolve_answer_mode(yes=opts.yes)
        if mode is AnswerMode.ALL_NO:
            return 0, 0  # every answer is no: keep every extra, successfully
        if mode is AnswerMode.ASK:
            confirmer = DeleteConfirmer(mode, entry)
    extras: list[tuple[str, bool]] = []
    for _key, m, loc in manifest.merge_join(
        manifest_keyed(manifest_path, sub), local_keyed(outpath, excludes)
    ):
        if m is None and loc is not None:
            rel, st, _sym = loc
            if rel != ".":
                extras.append((os.path.join(outpath, rel), stat_mod.S_ISDIR(st.st_mode)))
    errors, removed = remove_extras(extras, dryrun=opts.dryrun, confirm=confirmer)
    return (1 if errors else 0), removed


def _mirror_extras(
    manifest_path: str,
    outpath: str,
    sub: str | None,
    excludes: list[str],
    *,
    opts: Opts,
    entry: str,
    window_ns: int,
) -> int:
    """pull ``--delete``'s extras removal, followed by a directory-metadata
    re-settle: every removal bumps its parent directory's mtime, so when
    anything was actually removed the manifest metadata is applied once more -
    the gated apply touches exactly the directories the removals dirtied.
    Skipped under --data-only and --dry-run, which never apply metadata."""
    status, removed = _delete_extras(manifest_path, outpath, sub, excludes, opts=opts, entry=entry)
    if removed and not opts.dryrun and not opts.data_only:
        settle = apply_manifest(
            outpath, True, manifest_path, sub=sub, window_ns=window_ns, excludes=excludes
        )
        status = status or settle
    return status


def cmd_show(cfg: Config, entry: str, opts: Opts, file: str | None = None) -> int:
    if entry not in cfg.entries:
        err(f"no such entry: {entry}")
        return 1

    if file:
        # removeprefix, not lstrip: lstrip("./") strips *characters*, mangling a
        # dotfile (".bashrc" -> "bashrc") into the wrong S3 key.
        file = file.removeprefix("./")
        rel = f"{entry}/{file}"
    else:
        rel = entry

    assert cfg.store is not None
    return cfg.store.stream_object_to_stdout(rel, verbose=opts.verbose)


def cmd_status(cfg: Config, entry: str, opts: Opts, sub: str | None = None) -> int:
    entry_cfg = cfg.entries.get(entry)
    if not entry_cfg:
        err(f"no such entry: {entry}")
        return 1
    base_path: str = entry_cfg["path"]
    outpath = os.path.join(base_path, sub) if sub else base_path

    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        if not download_manifest(cfg, entry, manifest_path, opts.verbose):
            err(f"entry not found on S3: {entry}")
            return 1
        # Classify from the manifest (the record of the last push), not the
        # local filesystem: a directory entry whose local tree was deleted must
        # still map each record to its own child path (is_dir=False would fold
        # every record onto outpath and print duplicate/wrong lines).
        if sub is not None:
            sub_kind = _sub_kind_from_manifest(manifest_path, sub)
            if sub_kind == "missing":
                err(f"not found on S3: {entry}/{sub}")
                return 1
            is_dir = sub_kind == "dir"
        else:
            is_dir = _entry_kind_from_manifest(manifest_path) == "dir"
        excludes: list[str] = entry_cfg.get("excludes", [])
        use_color = _resolve_use_color(opts.color)
        window_ns = cfg.window_ns_for(entry)

        if not is_dir:
            # Single-file entry (or a file/symlink sub): one direct compare.
            for entry_obj in manifest.iter_manifest(manifest_path):
                res = manifest_target(entry_obj, outpath, is_dir, sub)
                if res is None:
                    continue
                target, _rel = res
                block = check_metadata(
                    target,
                    entry_obj,
                    opts.verbose,
                    window_ns,
                    use_color=use_color,
                    ignore_dir_mtime=True,
                )
                if block:
                    write_output(block)
            return 0

        # Directory tree: one streaming merge-join of the manifest against a
        # fresh walk decides everything - M (both sides, drifted), D
        # (manifest-only), A (local-only) - in key order, holding only the
        # current pair in memory. The walk's lstat/readlink feed the compare,
        # so no path is stat'd twice.
        for _key, m, loc in manifest.merge_join(
            manifest_keyed(manifest_path, sub), local_keyed(outpath, excludes)
        ):
            if m is not None:
                rel, entry_obj = m
                target = outpath if rel == "." else os.path.join(outpath, rel)
                if loc is None:
                    write_output(f"D {target}\n")
                    continue
                _rel, st, sym = loc
                diff = compare_to_stat(
                    entry_obj,
                    st,
                    sym,
                    window_ns=window_ns,
                    use_color=use_color,
                    ignore_dir_mtime=True,
                )
                block = format_diff_block(diff, target, opts.verbose)
                if block:
                    write_output(block)
            elif loc is not None:
                rel, _st, _sym = loc
                if rel != ".":
                    write_output(f"A {os.path.join(outpath, rel)}\n")

        return 0
    finally:
        os.unlink(manifest_path)


def _run_diff(left: str, right: str, label: str, opts: Opts) -> int:
    cmd = ["diff"]
    color_flag = _diff_color_flag(opts.color)
    if color_flag is not None:
        cmd.append(color_flag)
    cmd.extend(
        [
            "-u",
            "--label",
            f"a/{label}",
            "--label",
            f"b/{label}",
            "--",
            left,
            right,
        ]
    )
    echo_command(opts.verbose, cmd)
    return subprocess.run(cmd).returncode


def _write_leaf_type_diff(label: str, backup: str, local: str) -> None:
    write_output(f"--- a/{label}\n+++ b/{label}\n-{backup}\n+{local}\n")


def _local_leaf_description(path: str, mode: int) -> str:
    if stat_mod.S_ISLNK(mode):
        return f"symlink -> {os.readlink(path)!r}"
    if stat_mod.S_ISDIR(mode):
        return "directory"
    if stat_mod.S_ISREG(mode):
        return "regular file"
    return "special file"


def diff_single_file(
    cfg: Config,
    rel_key: str,
    label: str,
    localfile: str,
    opts: Opts,
    *,
    size: int | None = None,
) -> int:
    fd, tmppath = tempfile.mkstemp()
    os.close(fd)
    try:
        assert cfg.store is not None
        if not cfg.store.get_object(rel_key, tmppath, size=size, verbose=opts.verbose):
            err(f"not found on S3: {rel_key}")
            return 1
        try:
            local_mode = os.lstat(localfile).st_mode
        except FileNotFoundError:
            return 0 if _run_diff(tmppath, os.devnull, label, opts) == 0 else 1
        if not stat_mod.S_ISREG(local_mode):
            # Never let content diff follow a symlink that replaced a recorded
            # regular file: it could disclose an unrelated target's contents.
            _write_leaf_type_diff(
                label,
                "regular file",
                _local_leaf_description(localfile, local_mode),
            )
            return 1
        return 0 if _run_diff(tmppath, localfile, label, opts) == 0 else 1
    finally:
        os.unlink(tmppath)


def diff_backup(
    cfg: Config,
    rel_prefix: str,
    outpath: str,
    opts: Opts,
    manifest_path: str,
    *,
    entry: str,
    sub: str | None = None,
) -> int:
    excludes: list[str] = cfg.entries[entry].get("excludes", [])
    tmpdir = tempfile.mkdtemp()
    has_diff = 0

    try:
        assert cfg.store is not None
        result = cfg.store.sync_down(rel_prefix, tmpdir, verbose=opts.verbose)
        if result.returncode != 0:
            if result.stderr:
                write_stderr(result.stderr)
            return result.returncode

        # The manifest, not every object that happens to remain under the S3
        # prefix, defines the backup. Orphan objects can still exist (e.g. a
        # --meta-only push after a local delete, or an exclude added later);
        # diff must ignore them just as pull/status do.
        backup_files: set[str] = set()
        backup_symlinks: dict[str, str] = {}
        backup_special: dict[str, int] = {}
        for record in manifest.iter_manifest(manifest_path):
            rel = resolve_manifest_rel(record.path, sub)
            if rel is None or rel == ".":
                continue
            if record.is_file and record.sym_target is None:
                backup_files.add(rel)
            elif record.sym_target is not None:
                backup_symlinks[rel] = record.sym_target
            elif not record.is_dir:
                backup_special[rel] = record.mode

        for rel in sorted(backup_files):
            full = os.path.join(tmpdir, rel)
            if not os.path.isfile(full):
                err(f"expected backup object missing: {rel_prefix}/{rel}")
                has_diff = 1
                continue
            local = os.path.join(outpath, rel)
            try:
                local_mode = os.lstat(local).st_mode
            except FileNotFoundError:
                _run_diff(full, os.devnull, rel, opts)
                has_diff = 1
                continue
            if not stat_mod.S_ISREG(local_mode):
                _write_leaf_type_diff(
                    rel,
                    "regular file",
                    _local_leaf_description(local, local_mode),
                )
                has_diff = 1
                continue
            if _run_diff(full, local, rel, opts) != 0:
                has_diff = 1

        for rel, backup_target in sorted(backup_symlinks.items()):
            local = os.path.join(outpath, rel)
            try:
                local_mode = os.lstat(local).st_mode
            except FileNotFoundError:
                local_value = "missing"
            else:
                local_value = _local_leaf_description(local, local_mode)
                if stat_mod.S_ISLNK(local_mode) and os.readlink(local) == backup_target:
                    continue
            _write_leaf_type_diff(rel, f"symlink -> {backup_target!r}", local_value)
            has_diff = 1

        for rel, backup_mode in sorted(backup_special.items()):
            local = os.path.join(outpath, rel)
            try:
                local_mode = os.lstat(local).st_mode
            except FileNotFoundError:
                local_value = "missing"
            else:
                if stat_mod.S_IFMT(local_mode) == stat_mod.S_IFMT(backup_mode):
                    continue
                local_value = _local_leaf_description(local, local_mode)
            _write_leaf_type_diff(rel, "special file", local_value)
            has_diff = 1

        if sub is None:
            local_items = (
                localwalk.walk_tree(outpath, excludes)
                if os.path.isdir(outpath) and not os.path.islink(outpath)
                else ()
            )
            root_rel = "."
            rel_prefix_local = "./"
        else:
            root_rel = f"./{sub}"
            rel_prefix_local = f"./{sub}/"
            local_items = (
                localwalk.walk_tree(
                    outpath,
                    excludes,
                    root_rel=root_rel,
                    rel_prefix=rel_prefix_local,
                )
                if os.path.isdir(outpath) and not os.path.islink(outpath)
                else ()
            )

        for walk_rel, walk_st, _sym in local_items:
            if walk_rel == root_rel or stat_mod.S_ISDIR(walk_st.st_mode):
                continue
            rel = walk_rel.removeprefix(rel_prefix_local)
            local = os.path.join(outpath, rel)
            if rel in backup_files or rel in backup_symlinks or rel in backup_special:
                continue
            if stat_mod.S_ISREG(walk_st.st_mode):
                _run_diff(os.devnull, local, rel, opts)
            else:
                _write_leaf_type_diff(
                    rel,
                    "missing",
                    _local_leaf_description(local, walk_st.st_mode),
                )
            has_diff = 1

        return has_diff
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def cmd_diff(cfg: Config, entry: str, opts: Opts, file: str | None = None) -> int:
    entry_cfg = cfg.entries.get(entry)
    if not entry_cfg:
        err(f"no such entry: {entry}")
        return 1
    outpath: str = entry_cfg["path"]

    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        if not download_manifest(cfg, entry, manifest_path, opts.verbose):
            err(f"entry not found on S3: {entry}")
            return 1
        entry_is_dir = _entry_kind_from_manifest(manifest_path) == "dir"

        if file:
            if not entry_is_dir:
                err(f"sub path not allowed for single-file entry: {entry}")
                return 1
            file = file.removeprefix("./")
            kind = _sub_kind_from_manifest(manifest_path, file)
            if kind == "missing":
                err(f"not found on S3: {entry}/{file}")
                return 1
            local = os.path.join(outpath, *file.split("/"))
            if kind == "dir":
                return diff_backup(
                    cfg,
                    f"{entry}/{file}",
                    local,
                    opts,
                    manifest_path,
                    entry=entry,
                    sub=file,
                )
            if kind == "symlink":
                for record in manifest.iter_manifest(manifest_path):
                    if record.path.removeprefix("./") == file:
                        local_mode: int | None
                        try:
                            local_mode = os.lstat(local).st_mode
                        except FileNotFoundError:
                            local_mode = None
                            local_value = "missing"
                        else:
                            local_value = _local_leaf_description(local, local_mode)
                        if (
                            local_mode is not None
                            and stat_mod.S_ISLNK(local_mode)
                            and os.readlink(local) == record.sym_target
                        ):
                            return 0
                        _write_leaf_type_diff(
                            f"{entry}/{file}",
                            f"symlink -> {record.sym_target!r}",
                            local_value,
                        )
                        return 1
                raise manifest.ManifestError(f"missing symlink record for {entry}/{file}")
            if kind == "special":
                record = next(
                    record
                    for record in manifest.iter_manifest(manifest_path)
                    if record.path.removeprefix("./") == file
                )
                try:
                    local_mode = os.lstat(local).st_mode
                except FileNotFoundError:
                    local_value = "missing"
                else:
                    if stat_mod.S_IFMT(local_mode) == stat_mod.S_IFMT(record.mode):
                        return 0
                    local_value = _local_leaf_description(local, local_mode)
                _write_leaf_type_diff(f"{entry}/{file}", "special file", local_value)
                return 1
            size = next(
                (
                    record.size
                    for record in manifest.iter_manifest(manifest_path)
                    if record.path.removeprefix("./") == file
                ),
                None,
            )
            return diff_single_file(
                cfg,
                f"{entry}/{file}",
                f"{entry}/{file}",
                local,
                opts,
                size=size,
            )

        if entry_is_dir:
            return diff_backup(cfg, entry, outpath, opts, manifest_path, entry=entry)
        return diff_single_file(
            cfg,
            entry,
            entry,
            outpath,
            opts,
            size=_single_file_size(manifest_path),
        )
    finally:
        os.unlink(manifest_path)


# Objects in these storage classes reject get_object until manually restored,
# so a pull over them fails. (An INTELLIGENT_TIERING archive tier fails the
# same way but is invisible in listings - a documented verify limit.)
_ARCHIVED_CLASSES = ("GLACIER", "DEEP_ARCHIVE")


@dataclass
class _VerifyReport:
    """Finding accounting for one entry's verify. Errors mean the backup does
    not restore what the manifest promises (exit 1); warnings mean an object
    sits outside the backup (exit 2 via the global warning count); pending
    changes are informational only and leave the exit code alone."""

    entry: str
    file_records: int = 0
    objects: int = 0
    errors: int = 0
    warnings: int = 0
    pendings: int = 0

    def error(self, msg: str) -> None:
        err(f"{self.entry}: {msg}")
        self.errors += 1

    def warn(self, msg: str) -> None:
        note_warning(f"warning: {self.entry}: {msg}")
        self.warnings += 1

    def pending(self, msg: str) -> None:
        write_output(f"{self.entry}: pending change: {msg}\n")
        self.pendings += 1

    def finish(self) -> int:
        """Print the per-entry summary line - the record/object tallies double
        as a heartbeat for cron logs - and return the entry's exit status."""
        counts = f"{self.file_records} file record(s), {self.objects} data object(s)"
        if self.pendings:
            counts += f", {self.pendings} pending change(s)"
        if self.errors or self.warnings:
            write_output(
                f"{self.entry}: {self.errors} error(s), {self.warnings} warning(s) ({counts})\n"
            )
        else:
            write_output(f"{self.entry}: OK ({counts})\n")
        return 1 if self.errors else 0


class _ContentChecker:
    """The verify ``--checksum`` lane: compare local file content against the
    S3 ETag the listing (or head) already delivered - zero extra S3 calls.

    Hashing runs on a pool sized by ``max_concurrency`` (the sync compare
    itself is serial, but verify has no ordering constraint), with at most
    two hashes per worker in flight,
    and findings emitted in submission (key) order. A mismatch splits on the
    manifest stat: size+mtime still matching means the default push will never
    upload the edit (the size+mtime blind spot - an error), a drifted stat is
    an ordinary not-yet-pushed change (informational)."""

    def __init__(self, cfg: Config, entry: str, report: _VerifyReport):
        assert cfg.store is not None
        self._differs = cfg.store.etag_checker()
        self._window_ns = cfg.window_ns_for(entry)
        self._report = report
        self._size = cfg.store.compare_pool_size()
        self._pool: ThreadPoolExecutor | None = None
        self._queue: deque[tuple[Future[bool | None], ManifestEntry, os.stat_result, str]] = deque()

    def check(self, rel_key: str, local_path: str, record: ManifestEntry, obj: ObjectMeta) -> None:
        try:
            st = os.lstat(local_path)
        except OSError:
            return  # a kept deletion has no local counterpart: nothing to compare
        if not stat_mod.S_ISREG(st.st_mode):
            return  # a local type change is a status finding, not a backup defect

        def hash_one() -> bool | None:
            try:
                return self._differs(rel_key, local_path, obj.size, obj.etag)
            except OSError:
                return None  # vanished or unreadable mid-check: skip, not crash

        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=self._size, thread_name_prefix="s3bak-verify"
            )
        self._queue.append((self._pool.submit(hash_one), record, st, local_path))
        self._drain(self._size * 2)

    def _drain(self, limit: int) -> None:
        while len(self._queue) > limit:
            future, record, st, local_path = self._queue.popleft()
            if not future.result():
                continue
            if record.matches_stat(st, self._window_ns):
                self._report.error(
                    f"content differs but size+mtime match: {local_path}"
                    f" (push will not upload it; use push --checksum)"
                )
            else:
                self._report.pending(f"{local_path} (a push will upload it)")

    def close(self) -> None:
        self._drain(0)
        if self._pool is not None:
            self._pool.shutdown()


def _report_folder_object(report: _VerifyReport, url: str, obj: ObjectMeta) -> None:
    """A ``/``-terminated key - the manual-folder convention, never written by
    s3bak. Zero bytes is the marker the sync skips (restore unaffected); one
    carrying data would be a download to an impossible local path."""
    if obj.size == 0:
        report.warn(f"folder object: {url} (not created by s3bak; remove with aws s3 rm)")
    else:
        report.error(
            f"folder object with data: {url}"
            f" ({obj.size} bytes; a '/'-terminated key cannot restore to a local path)"
        )


def _check_archived(report: _VerifyReport, url: str, obj: ObjectMeta) -> bool:
    """Flag an object pull cannot download. Applies to every listed object -
    pull's listing-driven sync fetches unrecorded objects too."""
    if obj.storage_class in _ARCHIVED_CLASSES:
        report.error(
            f"storage class {obj.storage_class} blocks restore: {url}"
            f" (get_object fails until the object is restored from the archive)"
        )
        return True
    return False


def _verify_dir(
    cfg: Config,
    entry: str,
    report: _VerifyReport,
    manifest_path: str,
    sub: str | None,
    opts: Opts,
    local_base: str,
) -> None:
    """Merge-join the manifest records against the S3 listing - both ascend in
    key byte order, so one streaming pass checks the whole correspondence:
    every file record has its object (size intact, class restorable), every
    non-file record has none, and every object is accounted for."""
    assert cfg.store is not None
    rel_base = f"{entry}/{sub}" if sub else entry

    # An object at the tree's own key (the residue of a file that became this
    # directory) has no place in any restore: probe the exact key, the one spot
    # the slash-bounded listing cannot see.
    root_obj = cfg.store.head_object(rel_base, verbose=opts.verbose)
    if root_obj is not None:
        report.objects += 1
        report.error(
            f"type conflict: {cfg.prefix}/{rel_base}"
            f" (manifest records a directory, but a data object exists at its key)"
        )

    checker = _ContentChecker(cfg, entry, report) if opts.checksum else None
    # An unmatched object waits here while a directory record at key + "/"
    # can still arrive (siblings such as "key.txt" sort between the two, since
    # "." < "/"); once the join passes that key it settles as unrecorded.
    waiting: list[ObjectMeta] = []

    def settle(obj: ObjectMeta, *, conflict: bool) -> None:
        url = f"{cfg.prefix}/{rel_base}/{obj.key}"
        if conflict:
            report.error(
                f"type conflict: {url}"
                f" (manifest records a directory, but a data object exists at its key)"
            )
        else:
            report.warn(
                f"unrecorded object: {url} (not in the manifest; push --delete decides its fate)"
            )

    try:
        for key, record, obj in manifest.merge_join(
            manifest.iter_compare_records(manifest_path, sub=sub),
            ((o.key, o) for o in cfg.store.iter_objects(rel_base, verbose=opts.verbose)),
        ):
            if waiting:
                still: list[ObjectMeta] = []
                for w in waiting:
                    if key < w.key + "/":
                        still.append(w)
                    else:
                        settle(w, conflict=key == w.key + "/" and record is not None)
                waiting = still
            archived = False
            if obj is not None:
                report.objects += 1
                archived = _check_archived(report, f"{cfg.prefix}/{rel_base}/{obj.key}", obj)
            if record is not None and obj is not None:
                if key.endswith("/"):
                    # Only a directory record carries the trailing slash, and
                    # only a folder object can share its key.
                    _report_folder_object(report, f"{cfg.prefix}/{rel_base}/{obj.key}", obj)
                elif record.is_file and record.sym_target is None:
                    report.file_records += 1
                    if record.size != obj.size:
                        report.error(
                            f"size mismatch: {cfg.prefix}/{rel_base}/{obj.key}"
                            f" (manifest {record.size}, S3 {obj.size})"
                        )
                    elif checker is not None and not archived:
                        local_path = os.path.join(local_base, *key.split("/"))
                        checker.check(f"{rel_base}/{key}", local_path, record, obj)
                else:
                    kind = "symlink" if record.sym_target is not None else "special file"
                    report.error(
                        f"type conflict: {cfg.prefix}/{rel_base}/{obj.key}"
                        f" (manifest records a {kind}, but a data object exists at its key)"
                    )
            elif record is not None:
                if record.is_file and record.sym_target is None:
                    report.file_records += 1
                    report.error(
                        f"missing data object: {cfg.prefix}/{rel_base}/{key}"
                        f" (pull cannot restore it)"
                    )
                # Directory, symlink, and special records have no object by design.
            else:
                assert obj is not None
                if obj.key.endswith("/") or not obj.key:
                    # An empty relative key is a folder object at the tree's
                    # own key (`<rel_base>/`): the same manual-folder
                    # convention, at the one position that strips to "".
                    _report_folder_object(report, f"{cfg.prefix}/{rel_base}/{obj.key}", obj)
                else:
                    waiting.append(obj)
        for w in waiting:
            settle(w, conflict=False)
    finally:
        if checker is not None:
            checker.close()


def _verify_file_record(
    cfg: Config,
    entry: str,
    report: _VerifyReport,
    record: ManifestEntry,
    rel_key: str,
    local_path: str,
    opts: Opts,
) -> None:
    """Verify one recorded regular file (a single-file entry, or a file
    sub-path) against its exact object - a head probe, since a lone file has
    no listing to stream."""
    assert cfg.store is not None
    report.file_records += 1
    head = cfg.store.head_object(rel_key, verbose=opts.verbose)
    if head is None:
        report.error(f"missing data object: {cfg.prefix}/{rel_key} (pull cannot restore it)")
        return
    report.objects += 1
    if _check_archived(report, f"{cfg.prefix}/{rel_key}", head):
        return
    if record.size != head.size:
        report.error(
            f"size mismatch: {cfg.prefix}/{rel_key} (manifest {record.size}, S3 {head.size})"
        )
        return
    if opts.checksum:
        checker = _ContentChecker(cfg, entry, report)
        try:
            checker.check(rel_key, local_path, record, head)
        finally:
            checker.close()


def _verify_objectless_record(
    cfg: Config, report: _VerifyReport, rel_key: str, kind: str, opts: Opts
) -> None:
    """A recorded symlink or special file must have no data object at its key;
    one there is the residue of a type change and collides with the restore."""
    assert cfg.store is not None
    if cfg.store.head_object(rel_key, verbose=opts.verbose) is not None:
        report.objects += 1
        report.error(
            f"type conflict: {cfg.prefix}/{rel_key}"
            f" (manifest records a {kind}, but a data object exists at its key)"
        )


def cmd_verify(cfg: Config, entry: str, opts: Opts, sub: str | None = None) -> int:
    entry_cfg = cfg.entries.get(entry)
    if not entry_cfg:
        err(f"no such entry: {entry}")
        return 1
    base_path: str = entry_cfg["path"]
    report = _VerifyReport(entry)
    assert cfg.store is not None

    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        if not download_manifest(cfg, entry, manifest_path, opts.verbose):
            # No manifest: an unrecorded backup (interrupted push, --data-only)
            # and no backup at all are different emergencies - tell them apart.
            has_data = cfg.store.head_object(entry, verbose=opts.verbose) is not None or (
                next(iter(cfg.store.iter_objects(entry, verbose=opts.verbose)), None) is not None
            )
            if has_data:
                report.error(
                    "data objects exist but no manifest records them"
                    " (interrupted push? a push records them)"
                )
            else:
                report.error("no backup on S3 (entry never pushed)")
            return report.finish()

        entry_is_dir = _entry_kind_from_manifest(manifest_path) == "dir"
        if sub is not None:
            if not entry_is_dir:
                err(f"sub path not allowed for single-file entry: {entry}")
                return 1
            kind = _sub_kind_from_manifest(manifest_path, sub)
            if kind == "missing":
                err(f"not found on S3: {entry}/{sub}")
                return 1
            rel_key = f"{entry}/{sub}"
            local_path = os.path.join(base_path, *sub.split("/"))
            if kind == "dir":
                _verify_dir(cfg, entry, report, manifest_path, sub, opts, local_path)
            elif kind == "file":
                record = next(
                    r
                    for r in manifest.iter_manifest(manifest_path)
                    if r.path.removeprefix("./") == sub
                )
                _verify_file_record(cfg, entry, report, record, rel_key, local_path, opts)
            else:
                kind_name = "symlink" if kind == "symlink" else "special file"
                _verify_objectless_record(cfg, report, rel_key, kind_name, opts)
        elif entry_is_dir:
            _verify_dir(cfg, entry, report, manifest_path, None, opts, base_path)
        else:
            # A validated file-shaped manifest holds exactly one regular-file
            # record (validate_manifest), so there is nothing else to classify.
            record = next(manifest.iter_manifest(manifest_path))
            _verify_file_record(cfg, entry, report, record, entry, base_path, opts)
            # A file-shaped manifest records nothing below entry/, so anything
            # there is outside the backup - the residue of a directory that
            # became this file, or an out-of-band upload. A single-file pull
            # never lists (no restore collision), but nothing else can see
            # these objects, so verify sweeps the slash-bounded listing.
            for obj in cfg.store.iter_objects(entry, verbose=opts.verbose):
                report.objects += 1
                report.warn(
                    f"unrecorded object: {cfg.prefix}/{entry}/{obj.key}"
                    f" (not in the manifest; push --delete decides its fate)"
                )
        return report.finish()
    finally:
        os.unlink(manifest_path)


def verify_top_level(cfg: Config, opts: Opts) -> None:
    """The ``verify --all`` sweep: one non-recursive listing to inventory the
    prefix top level and warn about anything no configured entry accounts for -
    a stale manifest left behind by a removed entry, a data tree with no
    manifest, or a stray top-level object. Warnings only: nothing here breaks
    the restore of a configured entry."""
    assert cfg.store is not None
    objects, prefixes = cfg.store.list_top_level(verbose=opts.verbose)
    manifests = {
        name.removesuffix(manifest.MANIFEST_SUFFIX)
        for name in objects
        if name.endswith(manifest.MANIFEST_SUFFIX)
    }
    for name in sorted(manifests - cfg.entries.keys()):
        note_warning(
            f"warning: stale manifest (no configured entry):"
            f" {cfg.prefix}/{manifest.manifest_key(name)}"
        )
    for name in sorted(objects):
        # A name its own manifest accounts for is covered: configured, by the
        # per-entry verify; unconfigured, by the stale-manifest warning above.
        if name.endswith(manifest.MANIFEST_SUFFIX) or name in cfg.entries or name in manifests:
            continue
        note_warning(f"warning: top-level object outside any configured entry: {cfg.prefix}/{name}")
    for name in sorted(set(prefixes)):
        if name in cfg.entries or name in manifests:
            continue
        note_warning(
            f"warning: data tree without a manifest or configured entry: {cfg.prefix}/{name}/"
        )


def cmd_list(cfg: Config, opts: Opts) -> int:
    for key in sorted(cfg.entries.keys()):
        path = cfg.entries[key]["path"]
        write_output(f"{key:<20s} {path}\n")
    return 0


def show_entry_files(manifest_path: str, sub: str | None = None) -> None:
    for entry in manifest.iter_manifest(manifest_path):
        if sub is not None:
            rel = entry.path.removeprefix("./")
            if rel != sub and not rel.startswith(sub + "/"):
                continue
        display = entry.path
        if entry.sym_target:
            display = f"{entry.path} -> {entry.sym_target}"
        when = "" if entry.mtime_ns is None else _fmt_mtime(entry.mtime_ns)
        size = "" if entry.size is None else str(entry.size)
        write_output(
            f"{format(entry.mode, 'o'):<6s} {entry.owner:<8s} {entry.group:<8s} "
            f"{size:>8s}  {when}  {display}\n"
        )


def cmd_ls_remote(cfg: Config, opts: Opts, entry: str | None = None, sub: str | None = None) -> int:
    assert cfg.store is not None
    if entry is None:
        names, _prefixes = cfg.store.list_top_level(verbose=opts.verbose)
        for name in names:
            if name.endswith(manifest.MANIFEST_SUFFIX):
                write_output(f"{name.removesuffix(manifest.MANIFEST_SUFFIX)}\n")
        return 0

    if entry not in cfg.entries:
        err(f"no such entry: {entry}")
        return 1

    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        if not download_manifest(cfg, entry, manifest_path, opts.verbose):
            err(f"entry not found on S3: {entry}")
            return 1
        if sub is not None and _sub_kind_from_manifest(manifest_path, sub) == "missing":
            err(f"not found on S3: {entry}/{sub}")
            return 1
        show_entry_files(manifest_path, sub=sub)
        return 0
    finally:
        os.unlink(manifest_path)
