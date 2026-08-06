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
import signal
import stat as stat_mod
import subprocess
import tempfile
from collections import deque
from collections.abc import Callable, Iterator
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
from s3bak.console import IS_WINDOWS, console, is_junction, normalize_local_path
from s3bak.excludes import Excludes
from s3bak.manifest import ManifestEntry
from s3bak.restore import (
    apply_manifest,
    fs_alias_key,
    local_keyed,
    manifest_keyed,
    manifest_target,
    prepare_dir_conflicts,
    remove_extras,
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
    publish_journal_manifest,
    sync_compare,
    write_manifest_to_aws,
)

if TYPE_CHECKING:
    from boto3_s3 import FileFilter, FileInfo, PairFilter, SyncPair


def _run_hook(
    name: str, hook: list[str] | None, opts: Opts, journal_path: str | None = None
) -> int:
    """Run one configured hook directly, without a command shell. A failing
    hook's status propagates (the documented 3+ lane), normalized where it
    would collide with s3bak's own exit codes: 2 is reserved for a
    warnings-only run, so it maps to 1, and a signal death (negative
    returncode) becomes the conventional 128+N instead of leaking a negative
    value into sys.exit.

    ``journal_path`` is the just-produced push journal (see journal.md),
    passed only by the journal-driven directory and sub-path pushes; every
    other caller leaves it None. When set, the hook's environment gets
    ``S3BAK_JOURNAL`` pointing at it - the file is still on disk (the caller
    unlinks it only after this call returns). Entries push concurrently in a
    thread pool, so the variable is passed through ``env=`` rather than
    mutating ``os.environ``; when None, no ``env`` argument is passed at all
    and the hook simply inherits the process environment unchanged."""
    if not hook:
        return 0
    if opts.dryrun:
        console.out(f"(dry-run) would run {name}: {hook!r}\n")
        return 0
    if opts.verbose:
        console.diag(f"+ {name}: {hook!r}\n")
    # Hooks are non-interactive: entries push concurrently and a --delete
    # confirmation may be reading stdin on another thread, so a hook that also
    # read stdin would steal the answer. Detach it from the terminal.
    if journal_path is None:
        rc = subprocess.run(hook, shell=False, stdin=subprocess.DEVNULL).returncode
    else:
        rc = subprocess.run(
            hook,
            shell=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "S3BAK_JOURNAL": journal_path},
        ).returncode
    if rc == 0:
        return 0
    console.err(f"{name} failed (exit {rc}): {hook!r}")
    if rc < 0:
        return 128 - rc  # killed by signal N -> 128+N
    return 1 if rc == 2 else rc


def cmd_hook(cfg: Config, entry: str, opts: Opts, *, which: str) -> int:
    """Run one configured hook on demand (``s3bak hook pre|post <entry>``),
    outside any push - re-running an off-site copy after the far side
    changed, or testing a dump script. The hook executes under the same
    contract as a push-run hook (``_run_hook``: argument vector, no shell,
    stdin detached, the same exit normalization), with ``S3BAK_JOURNAL``
    unset - there is no push, hence no journal, which the hook contract
    reads as "no per-file detail; assume anything may have changed". An
    entry without the named hook fails: naming the hook is an instruction,
    and silence would read as success."""
    entry_cfg = cfg.entries.get(entry)
    if not entry_cfg:
        console.err(f"no such entry: {entry}")
        return 1
    name = f"{which}_hook"
    hook: list[str] | None = entry_cfg.get(name)
    if not hook:
        console.err(f"{entry}: no {name} configured")
        return 1
    return _run_hook(name, hook, opts)


def upload_manifest(cfg: Config, entry: str, target: str, opts: Opts) -> int:
    """Write the single-file entry's one-record manifest from a fresh lstat,
    then run the entry's post_hook. An ordinary directory push publishes its
    journal instead."""
    post_hook: list[str] | None = cfg.entries[entry].get("post_hook")

    if opts.dryrun:
        console.out(f"(dry-run) would update manifest: {manifest.manifest_key(entry)}\n")
        # The record write and validation run against a local temp file: the
        # rehearsal fails where the real push would, skipping only the upload.
        write_manifest_to_aws(cfg, entry, target, opts.verbose, upload=False)
        return _run_hook("post_hook", post_hook, opts)

    write_manifest_to_aws(cfg, entry, target, opts.verbose)

    return _run_hook("post_hook", post_hook, opts)


@dataclass
class _PushDeletePlan:
    """How this push treats S3 orphans: the sync's delete-lane value, plus
    the confirmation for objectless record orphans (``record_delete``, the
    journal emitter's callback for a vanished directory / symlink / special
    file whose record is the whole backup). What each answer means for the
    manifest is the journal emitter's business (a confirmed deletion journals
    its record's drop at the decision point)."""

    lane: bool | FileFilter  # the per-orphan decision, or False for "no --delete"
    confirmer: DeleteConfirmer | None  # --delete without --yes (asked or auto-n)
    walker: localwalk.ManifestWalker | None = None  # the sync's local walker (--delete only)
    old_manifest: str | None = None  # set by the caller once downloaded
    refused: int = 0  # candidates refused because the local scan was incomplete
    record_delete: Callable[[str, ManifestEntry], bool] | None = None
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


def _record_candidate_display(cfg: Config, entry: str, rel: str, e: ManifestEntry) -> str:
    """How a record-only delete candidate is shown (prompt and delete line):
    a directory record carries a trailing slash, like the S3 key its children
    would sort under."""
    return f"{cfg.prefix}/{entry}/{rel}/" if e.is_dir else f"{cfg.prefix}/{entry}/{rel}"


def _record_candidate_kind(e: ManifestEntry) -> str:
    if e.is_dir:
        return "directory record"
    if e.sym_target is not None:
        return "symlink record"
    return "special-file record"


def _keep_orphan(_info: FileInfo) -> bool:
    """The delete lane's decision without ``--delete``: keep every S3 orphan.

    Passed to the sync as a filter rather than as a flat False so the journal
    still observes the key (see ``_journal_delete_lane``)."""
    return False


def _journal_delete_lane(journal: PushJournal, plan: _PushDeletePlan) -> FileFilter:
    """The delete lane the sync runs, wrapped so the journal sees every S3-only
    key - with or without ``--delete``.

    The journal has to tell "no object at this key" from "an object the run
    was not allowed to touch": a record the pair stream never keys is stale
    and any push retires it (PushJournal._skip_over), so an object that IS
    there must reach the journal rather than be skipped over. Without
    ``--delete`` the wrapped decision is a flat no, so the lane observes and
    deletes nothing."""
    lane = _keep_orphan if plan.lane is False else plan.lane
    # A --delete plan always resolves its lane to a callable (_plan_push_deletes
    # replaces the placeholder True before returning). Assert rather than fall
    # back: silently reading a True lane as "keep" would turn an unattended
    # --yes run into a no-op instead of failing.
    assert not isinstance(lane, bool), "a --delete lane must be a callable decision"
    return journal.observe_delete(lane)


def _plan_push_deletes(
    cfg: Config, entry: str, sub: str | None, opts: Opts, walker: localwalk.ManifestWalker
) -> _PushDeletePlan:
    if not opts.delete:
        return _PushDeletePlan(lane=False, confirmer=None)
    if opts.dryrun or resolve_answer_mode(yes=opts.yes) is AnswerMode.ALL_YES:
        # Report (dry run) or delete (--yes) every candidate the completeness
        # gate admits. The gate callable never prompts, so it is safe under
        # dryrun too (the library invokes a callable there as well).
        plan = _PushDeletePlan(lane=True, confirmer=None, walker=walker)
        plan.lane = lambda info: plan.allow()

        def report_record(rel: str, e: ManifestEntry) -> bool:
            # --yes auto-confirms, a dry run reports; both drop the candidate
            # through this one path, so the unattended run and its rehearsal
            # cannot diverge (there is no separate mirror lane).
            if not plan.allow():
                return False
            marker = "(dry-run) " if opts.dryrun else ""
            console.out(f"{marker}delete record: {_record_candidate_display(cfg, entry, rel, e)}\n")
            return True

        plan.record_delete = report_record
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

    def confirm_record(rel: str, e: ManifestEntry) -> bool:
        # A record with no object: the record IS the backup, so the prompt
        # says what kind it is. Confirmed drops print a delete line (the
        # store prints one for each deleted object; records have no store
        # call, so the emitter's confirmation is where it belongs).
        if not plan.allow():
            return False
        display = _record_candidate_display(cfg, entry, rel, e)
        if not confirmer.confirm(f"{display} ({_record_candidate_kind(e)})"):
            return False
        console.out(f"delete record: {display}\n")
        return True

    plan = _PushDeletePlan(lane=decide, confirmer=confirmer, walker=walker)
    plan.record_delete = confirm_record
    return plan


def _warn_refused_deletes(entry: str, plan: _PushDeletePlan, journal: PushJournal) -> None:
    """After the sync: say why deletion candidates were kept when the local
    scan was incomplete (see _PushDeletePlan.allow). The journal counts the
    record-only candidates its own gate suppressed before they could reach
    the plan's callback."""
    refused = plan.refused + journal.refused_records
    if refused:
        console.warn(
            f"warning: {entry}: the local scan skipped unreadable or vanished paths;"
            f" kept {refused} deletion candidate(s) and every manifest record"
        )


def _warn_aborted_push(entry: str) -> None:
    """Report a q answer, and what it leaves behind: the manifest is not
    rewritten, so whatever the run had already done to S3 - objects uploaded,
    confirmed deletions already executed - is not reflected in it. Both
    directions settle on the next push of the entry, without --delete: it
    records the uploads and retires the records whose objects are gone."""
    console.err(
        f"{entry}: aborted; the manifest was not rewritten, so it may no longer match S3"
        " - push this entry again to settle it"
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
    for rel, recorded in journal.iter_pending_object_deletes():
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
    return result.returncode, result.returncode == 0


def _delete_exact_root_object(
    cfg: Config, entry: str, plan: _PushDeletePlan, opts: Opts
) -> tuple[int, bool]:
    """A directory push's slash-bounded sync lists ``entry/`` and so never sees
    an object at the entry's OWN key ``entry`` (the residue of a former file, or
    an out-of-band write). verify flags it as a root type conflict, but the
    delete lane can't reach it. Under --delete, probe it and offer it for
    deletion out-of-lane - through the same confirmer / completeness gate as the
    kind-conflict objects. Returns ``(status, deleted_any)``."""
    assert cfg.store is not None
    if cfg.store.head_object(entry, verbose=opts.verbose) is None:
        return 0, False
    if not plan.allow():
        return 0, False
    if plan.confirmer is not None and not plan.confirmer.confirm(
        f"{cfg.prefix}/{entry} (not in manifest)"
    ):
        return 0, False
    result = cfg.store.delete_objects([entry], dryrun=opts.dryrun, verbose=opts.verbose)
    return result.returncode, result.returncode == 0


def _reject_symlinked_sub_ancestors(base: str, sub: str) -> str | None:
    """Return an error message if ``base`` itself or any ancestor of ``sub`` (the
    components between ``base`` and the final one) is a symlink, a Windows
    directory junction, another non-directory, or otherwise inaccessible, else
    None. ``base`` is checked first, then each ancestor shallowest-first, so
    the outermost symlink is caught before any lstat resolves through it -
    including a relocated entry root (``base`` a symlink), through which
    `pull entry/file` would otherwise write outside the entry.

    A *genuinely missing* component (ENOENT) is not an error (no local state to
    read, nothing resolves through it; the caller's own existence checks and the
    --delete manifest path handle it). Any OTHER OS error - EACCES on an
    unsearchable directory, ELOOP, ENOTDIR - is a hard error: it must never be
    mistaken for "absent", which on a push --delete would drop a backup the walk
    simply could not see."""
    to_check = [base]
    acc = base
    for part in sub.split("/")[:-1]:
        acc = os.path.join(acc, part)
        to_check.append(acc)
    for path in to_check:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError as e:
            return f"cannot access sub path ancestor {path}: {e}"
        # A junction lstats as an ordinary directory (Windows does not model
        # it as a symlink), so it needs its own check alongside S_ISLNK -
        # otherwise it would sail through as if it were a real directory.
        if stat_mod.S_ISLNK(st.st_mode) or is_junction(st):
            return f"sub path crosses a symlink or junction ancestor, not allowed: {path}"
        if not stat_mod.S_ISDIR(st.st_mode):
            return f"sub path crosses a non-directory ancestor, not allowed: {path}"
    # The ancestors are accessible directories; probe the final target too (its
    # type is unrestricted). This catches a deepest ancestor that lstat'd fine
    # but is itself unsearchable (mode 000), whose EACCES would otherwise only
    # surface mid-operation.
    final = os.path.join(base, *sub.split("/"))
    try:
        os.lstat(final)
    except FileNotFoundError:
        return None  # absent target: fine (created on restore, or a gone --delete path)
    except OSError as e:
        return f"cannot access sub path ancestor {os.path.dirname(final)}: {e}"
    return None


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

    # Reject a sub whose ancestor chain crosses a symlink before reading any
    # local state: os.lstat(target_root/sub) resolves ancestor symlinks, so
    # e.g. `push entry/link/passwd` with `link -> /etc` would upload /etc/passwd
    # as entry/link/passwd and then leave it unrecorded (the ancestor records as
    # a symlink-shaped directory and fails manifest validation). The final
    # component may itself be a symlink (backed up as one), so it is not checked.
    ancestor_error = _reject_symlinked_sub_ancestors(target_root, sub)
    if ancestor_error is not None:
        console.err(ancestor_error)
        return 1

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
            console.err(f"sub path not allowed for single-file entry: {entry}")
            return 1
        if have_manifest:
            plan.old_manifest = manifest_path

        # os.path.lexists swallows EACCES (an unsearchable parent) as "absent",
        # which under --delete would delete a live backup for a path we simply
        # could not reach. Distinguish a genuine ENOENT from any other error.
        sub_st: os.stat_result | None
        try:
            sub_st = os.lstat(local_sub)
        except FileNotFoundError:
            sub_st = None
        except OSError as e:
            console.err(f"cannot access sub path: {local_sub}: {e}")
            return 1

        # The named target under the entry's excludes (docs/excludes.md):
        # naming an excluded path does not override the exclude. A present
        # target is judged by its actual kind - a directory counts as
        # invisible only when the filtered walk yields NOTHING (its own
        # record included), so a partially excluded directory still pushes
        # normally below. An absent target has no kind to consult, so either
        # spelling matching means the operator's config excludes the name,
        # and exclusion wins over "missing".
        ex = Excludes(excludes)
        sub_anchor = os.path.abspath(local_sub).replace(os.sep, "/")
        if sub_st is not None and stat_mod.S_ISDIR(sub_st.st_mode):
            walk = localwalk.walk_tree(
                local_sub, excludes, root_rel=f"./{sub}", rel_prefix=f"./{sub}/"
            )
            nothing_visible = next(iter(walk), None) is None
        elif sub_st is not None:
            nothing_visible = ex.excluded(sub, sub_anchor)
        else:
            nothing_visible = ex.excluded(sub, sub_anchor) or ex.excluded(
                f"{sub}/", sub_anchor + "/"
            )

        if sub_st is None or nothing_visible:
            if not opts.delete:
                if nothing_visible:
                    # Ignoring an excluded path is the rule, not an error
                    # (the same silence as an entry push that skips it).
                    return 0
                console.err(
                    f"local path does not exist (use --delete to remove its backup): {local_sub}"
                )
                return 1
            # The full deletion: one confirmation covers the subtree's
            # objects and manifest records together.
            if not opts.dryrun and not confirm_subtree_delete(
                resolve_answer_mode(yes=opts.yes), entry, s3_sub_path
            ):
                console.err(f"backup subtree not deleted (answer y, or use --yes): {s3_sub_path}")
                return 1
            result = cfg.store.delete_subtree(sub_rel, dryrun=opts.dryrun, verbose=opts.verbose)
            if result.returncode != 0:
                return result.returncode
            did_work = result.results > 0
            did_work = drop_subtree_records(cfg, entry, old_manifest, sub, opts) or did_work
            return _run_hook("post_hook", post_hook, opts) if did_work else 0

        st = sub_st
        is_link = stat_mod.S_ISLNK(st.st_mode)
        is_dir_sub = not is_link and os.path.isdir(local_sub)
        if not (is_link or is_dir_sub or stat_mod.S_ISREG(st.st_mode)):
            console.err(f"sub path must be a regular file, directory, or symlink: {local_sub}")
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
                dest_listed=is_dir_sub,
                delete_mode=opts.delete and is_dir_sub,
                record_delete=plan.record_delete,
            )
            # False until the journal's stream truly completed: a sync that
            # errored or an exception mid-push parks the cursor mid-manifest,
            # and close()'s drain must not judge (or offer) the unreached
            # tail. Only the directory branch runs delete_mode, but the flag
            # is kept honest for every branch.
            stream_complete = False
            try:
                if old_manifest is None:
                    # First-ever manifest born from a sub-path push: record the
                    # entry root so the manifest keeps its dir-entry shape and
                    # the root's metadata restores on pull.
                    journal.record_root(os.lstat(target_root))
                # Ancestor records for sub's parents, so their metadata
                # restores on pull; only a missing or drifted ancestor
                # journals. An excluded ancestor stays unrecorded (a parent
                # record is optional, docs/excludes.md).
                acc = target_root
                rel_acc: str | None = None
                for part in sub.split("/")[:-1]:
                    acc = os.path.join(acc, part)
                    rel_acc = part if rel_acc is None else f"{rel_acc}/{part}"
                    if ex.excluded(f"{rel_acc}/", os.path.abspath(acc).replace(os.sep, "/") + "/"):
                        continue
                    journal.record_ancestor(rel_acc, os.lstat(acc))

                if is_link:
                    # symlink: upload nothing; the manifest record IS the backup.
                    journal.record_target(sub, st, os.readlink(local_sub))
                    stream_complete = True
                elif is_dir_sub:
                    result = cfg.store.sync_up(
                        local_sub,
                        sub_rel,
                        walker=walker,
                        compare=journal.update_filter,
                        create=journal.create_filter,
                        delete=_journal_delete_lane(journal, plan),
                        dryrun=opts.dryrun,
                        verbose=opts.verbose,
                    )
                    stream_complete = result.returncode == 0
                    if result.returncode != 0:
                        return result.returncode
                    if result.results > 0:
                        did_work = True
                else:
                    # Regular file: an explicit sub-path push always uploads,
                    # and always re-records - naming the path is the
                    # instruction to back up its current state.
                    if opts.dryrun:
                        console.out(f"(dry-run) upload: {local_sub} -> {s3_sub_path}\n")
                        did_work = True
                    else:
                        result = cfg.store.put_object(sub_rel, local_sub, verbose=opts.verbose)
                        if result.returncode != 0:
                            return result.returncode
                        if result.results > 0:
                            did_work = True
                    journal.record_target(sub, st, None)
                    stream_complete = True
            finally:
                if not stream_complete:
                    # Gate close()'s drain: the unreached records are no
                    # evidence of deletion (see the directory push's twin).
                    walker.scan_incomplete = True
                journal.close()
            if journal.pending_object_deletes > 0:
                st_del, conflict_deleted = _delete_conflict_objects(cfg, entry, plan, journal, opts)
                if st_del != 0:
                    return st_del
            # After the conflict candidates: their refusals (incomplete scan)
            # must count in the summary too.
            _warn_refused_deletes(entry, plan, journal)
            if journal.has_events:
                publish_journal_manifest(cfg, entry, old_manifest, journal_path, opts)
                did_work = True
            if conflict_deleted:
                did_work = True
            # The journal must still be on disk for post_hook (S3BAK_JOURNAL);
            # it is unlinked in the finally below, after the hook returns.
            return _run_hook("post_hook", post_hook, opts, journal_path) if did_work else 0
        finally:
            os.unlink(journal_path)
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
    recorded size - an interrupted deletion or an S3-side delete leaves a
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


def _single_file_manifest_matches(manifest_path: str, target: str, window_ns: int) -> bool:
    """Whether the already-downloaded manifest describes this single-file
    entry's current state: the record names the configured basename, its
    permission bits match, AND its size+mtime match (within ``window_ns``).

    The size+mtime part keeps --checksum's manifest self-healing on par with the
    default push: a content-equal file whose mtime drifted out of the window
    still refreshes the record, so status settles and pull restores the current
    mtime, instead of the manifest staying stale forever (--checksum never
    re-transfers a content-equal file)."""
    record = next(manifest.iter_manifest(manifest_path))
    if record.path != os.path.basename(target):
        return False
    st = os.lstat(target)
    return not mode_differs(record, st) and record.matches_stat(st, window_ns)


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
        console.err(
            f"{entry}: the backup records a {old_kind} but the local path is now a"
            f" {new_kind}; push --delete replaces the old backup"
        )
        return 1
    display = f"{cfg.prefix}/{entry}"
    if not opts.dryrun and not confirm_subtree_delete(
        resolve_answer_mode(yes=opts.yes), entry, display
    ):
        console.err(f"old backup not deleted (answer y, or use --yes): {display}")
        return 1
    result = cfg.store.delete_subtree(entry, dryrun=opts.dryrun, verbose=opts.verbose)
    if result.returncode != 0:
        return result.returncode
    return 0


def _delete_file_entry_strays(cfg: Config, entry: str, opts: Opts) -> tuple[int, int]:
    """``push --delete`` for a single-file entry: offer the objects under
    ``entry/`` for deletion. A file-shaped manifest records only the entry's
    own key, so anything below ``entry/`` is outside the backup - the residue
    of an entry that used to be a directory, or an out-of-band upload - and
    would otherwise be invisible to every command but verify. The same
    per-object confirmation as the directory delete lane; the manifest is not
    touched (these keys have no records). Returns ``(status, results)`` -
    ``results`` is the number of delete lines printed (0 means nothing done)."""
    assert cfg.store is not None
    candidates = (f"{entry}/{o.key}" for o in cfg.store.iter_objects(entry, verbose=opts.verbose))
    if opts.dryrun or resolve_answer_mode(yes=opts.yes) is AnswerMode.ALL_YES:
        result = cfg.store.delete_objects(candidates, dryrun=opts.dryrun, verbose=opts.verbose)
    else:
        mode = resolve_answer_mode(yes=opts.yes)
        if mode is AnswerMode.ALL_NO:
            # Every answer is no: keep everything (each object returns as a
            # candidate on the next --delete).
            return 0, 0
        doomed: list[str] = []
        confirmer = DeleteConfirmer(mode, entry)
        for rel in candidates:
            if confirmer.confirm(f"{cfg.prefix}/{rel} (not in manifest)"):
                doomed.append(rel)
        if not doomed:
            return 0, 0
        result = cfg.store.delete_objects(doomed, verbose=opts.verbose)
    return result.returncode, result.results


def cmd_push(cfg: Config, entry: str, opts: Opts, sub: str | None = None) -> int:
    entry_cfg = cfg.entries.get(entry)
    if not entry_cfg:
        console.err(f"no such entry: {entry}")
        return 1
    target: str = entry_cfg["path"]
    target_root = normalize_local_path(target)

    excludes: list[str] = entry_cfg.get("excludes", [])

    # Hook contract: pre_hook runs before every push attempt. post_hook is
    # deliberately asymmetric - it runs only after a push that did work, i.e.
    # that transferred data and/or refreshed the manifest (see upload_manifest
    # and _push_sub). A pure no-op push runs no post_hook on purpose, so
    # side-effecting hooks (e.g. rclone) do not fire when nothing changed;
    # `s3bak hook post <entry>` runs the hook on demand. By design, not a bug.
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
            console.err(f"target does not exist: {target}")
            return 1
    else:
        mode = os.lstat(target_root).st_mode
        if stat_mod.S_ISLNK(mode):
            console.err(f"entry path is a symlink, which is not allowed as an entry: {target}")
            return 1
        if not (stat_mod.S_ISREG(mode) or stat_mod.S_ISDIR(mode)):
            console.err(f"entry path must be a regular file or directory: {target}")
            return 1

    if sub is not None:
        if os.path.lexists(target_root) and not os.path.isdir(target_root):
            console.err(f"sub path not allowed for single-file entry: {entry}")
            return 1
        post_hook_sub: list[str] | None = entry_cfg.get("post_hook")
        try:
            return _push_sub(cfg, entry, post_hook_sub, target_root, sub, excludes, opts)
        except DeletionAbortedError:
            _warn_aborted_push(entry)
            return 1

    results = 0  # count of upload/delete lines printed this push
    refresh_manifest = False
    assert cfg.store is not None

    walker = localwalk.sync_walker(excludes)
    plan = _plan_push_deletes(cfg, entry, None, opts, walker)
    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        # Every push downloads and validates the manifest first: an ordinary
        # push compares against it, any push uses it to notice objectless
        # tree changes or an entry kind change, and a damaged manifest must
        # abort the push before anything on S3 moves. All of that is
        # read-only, so it runs under --dry-run too - a rehearsal surfaces
        # problems here.
        have_manifest = download_manifest(cfg, entry, manifest_path, opts.verbose)
        is_dir_target = os.path.isdir(target)
        if have_manifest and (_entry_kind_from_manifest(manifest_path) == "dir") != is_dir_target:
            st = _migrate_entry_kind(cfg, entry, is_dir_target, opts)
            if st != 0:
                return st
            have_manifest = False  # the old backup is gone: record from scratch
        if have_manifest:
            plan.old_manifest = manifest_path

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
                    dest_listed=True,
                    delete_mode=opts.delete,
                    record_delete=plan.record_delete,
                )
                sync_ok = False
                try:
                    result = cfg.store.sync_up(
                        target,
                        entry,
                        walker=walker,
                        compare=journal.update_filter,
                        create=journal.create_filter,
                        delete=_journal_delete_lane(journal, plan),
                        dryrun=opts.dryrun,
                        verbose=opts.verbose,
                    )
                    sync_ok = result.returncode == 0
                finally:
                    if not sync_ok:
                        # The sync stopped mid-stream (an error or an
                        # interrupt, not a walk warning), so the cursor's
                        # unreached tail is no evidence of deletion. Gate
                        # close()'s drain like any other partial view: it
                        # must keep - and ask about - nothing.
                        walker.scan_incomplete = True
                    # Flush the journal (and release the old-manifest handle the
                    # cursor holds open - an open file cannot be removed on
                    # Windows) whether or not the sync succeeded.
                    journal.close()
                if result.returncode != 0:
                    return result.returncode
                results = result.results
                if journal.pending_object_deletes > 0:
                    st_del, conflict_deleted = _delete_conflict_objects(
                        cfg, entry, plan, journal, opts
                    )
                    if st_del != 0:
                        return st_del
                if opts.delete:
                    # The object at the entry's own key is invisible to the
                    # slash-bounded delete lane; retire it here (verify flags it).
                    st_root, root_deleted = _delete_exact_root_object(cfg, entry, plan, opts)
                    if st_root != 0:
                        return st_root
                    conflict_deleted = conflict_deleted or root_deleted
                # After the conflict candidates: their refusals (incomplete
                # scan) must count in the summary too.
                _warn_refused_deletes(entry, plan, journal)
                # The rewrite condition: at least one real event (+/!/-). A
                # first push journals everything (the root included); a pure
                # no-op push journals nothing; a --delete run whose record
                # candidates were all kept leaves only no-change lines, which
                # must not republish an identical manifest.
                if journal.has_events:
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
                if results or refresh_manifest or conflict_deleted:
                    post_hook: list[str] | None = entry_cfg.get("post_hook")
                    # The journal must still be on disk for post_hook
                    # (S3BAK_JOURNAL); it is unlinked in the finally below,
                    # after the hook returns.
                    return _run_hook("post_hook", post_hook, opts, journal_path)
                return 0
            finally:
                os.unlink(journal_path)
        else:
            needs_upload, mode_drifted = _single_file_compare(
                cfg, entry, target, opts, manifest_path if have_manifest else None
            )
            if needs_upload:
                # Single-file entry that fails the size+mtime check against its
                # manifest (or the --checksum ETag comparison), or was never
                # pushed: upload it.
                if opts.dryrun:
                    console.out(f"(dry-run) upload: {target} -> {cfg.prefix}/{entry}\n")
                    results = 1
                else:
                    result = cfg.store.put_object(entry, target, verbose=opts.verbose)
                    if result.returncode != 0:
                        return result.returncode
                    results = result.results
                refresh_manifest = results > 0
            else:
                if opts.checksum:
                    # ETag equality can skip an already-present data object even
                    # when its manifest was deleted, still names an older
                    # configured basename, or records a stale mode / mtime.
                    refresh_manifest = not have_manifest or not _single_file_manifest_matches(
                        manifest_path, target, cfg.window_ns_for(entry)
                    )
                else:
                    refresh_manifest = mode_drifted
            if opts.delete:
                # A single-file entry has no sync listing, so its --delete lane
                # is this explicit sweep of entry/ (see _delete_file_entry_strays).
                st, stray_count = _delete_file_entry_strays(cfg, entry, opts)
                if stray_count:
                    # Deletions are work: refresh the manifest (a no-op rewrite
                    # of the single record) so post_hook fires, as a directory
                    # delete-only push would.
                    refresh_manifest = True
                if st != 0:
                    return st

        # Single-file refresh: after an upload, a mode drift, or a stray
        # deletion (a no-op rewrite of the one record, so post_hook fires as
        # a directory delete-only push would). An mtime drift inside the
        # window does not refresh an existing manifest (the window is a
        # rounding tolerance).
        if refresh_manifest:
            st = upload_manifest(cfg, entry, target, opts)
            if st != 0:
                return st

        return 0
    except DeletionAbortedError:
        _warn_aborted_push(entry)
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
    manifest_path: str, outpath: str, is_dir: bool, sub: str | None, window_ns: int, ex: Excludes
) -> bool:
    """True iff every manifest record the pull would touch matches the local
    filesystem. Excluded records are outside the comparison - the pull skips
    them in full (docs/excludes.md) - so they cannot hold the gate open.

    Returning True means 'boto3-s3 sync' would copy nothing AND apply_manifest
    would change nothing - so both can be skipped.
    """
    for entry in manifest.iter_manifest(manifest_path):
        res = manifest_target(entry, outpath, is_dir, sub)
        if res is None:
            continue
        target, rel = res
        # The entry root ("." at entry scope, or a single-file entry's one
        # record) is never excluded; a SUB's own record is judged.
        base = "" if sub is None and (rel == "." or not is_dir) else rel
        if sub is not None:
            base = sub if rel == "." else f"{sub}/{rel}"
        if base:
            key = base + "/" if entry.is_dir else base
            anchor = os.path.abspath(target).replace(os.sep, "/") + ("/" if entry.is_dir else "")
            if ex.excluded(key, anchor):
                continue
        if not compare_to_local(entry, target, window_ns=window_ns).is_match:
            return False
    return True


def _pull_exclude_lanes(
    ex: Excludes, sub: str | None, outpath: str, compare: PairFilter | None
) -> tuple[FileFilter, PairFilter | None]:
    """Veto excluded keys in pull's download lanes (docs/excludes.md): a
    create-lane key under an excluded path is never downloaded, and an
    excluded both-sides pair is left untouched without consulting the
    stat/content compare (whose streaming cursor self-heals over keys it is
    not asked about). Keys are re-anchored at the entry root, where the
    patterns are defined; anchored (absolute) patterns match the restore
    destination's absolute path - aws-cli's join-onto-root semantics."""

    def excluded_key(compare_key: str) -> bool:
        key = f"{sub}/{compare_key}" if sub else compare_key
        anchor = os.path.abspath(os.path.join(outpath, compare_key.replace("/", os.sep)))
        return ex.excluded(key, anchor.replace(os.sep, "/"))

    def create(info: FileInfo) -> bool:
        assert info.compare_key is not None  # the sync stamps every listed entry
        return not excluded_key(info.compare_key)

    if compare is None:
        return create, None

    inner = compare

    def update(pair: SyncPair) -> bool:
        if excluded_key(pair.compare_key):
            return False
        return inner(pair)

    return create, update


def _manifest_restore_conflict(manifest_path: str, sub: str | None) -> str | None:
    """Return the entry-relative path of the first non-directory record (a file
    or symlink) in the pulled range that another record contradicts by treating
    it as a directory or filling it with descendants, else None.

    Such a manifest cannot be materialized: pulling it restores some records and
    then the deferred symlink/directory replacement rmtree's (or removes) the
    just-restored subtree - taking unrecorded local data with it - before failing.
    pull and verify therefore fail closed on it (push --delete prunes the stale
    records). Streams the manifest in sort order with a stack of "blockers" (the
    non-directory records whose descendant key range is still open), the shape
    the merge's restorability warning uses. Works in ENTRY-RELATIVE space
    (not sub-rebased): a conflict AT the sub root itself - a symlink ``./d`` and a
    directory ``./d`` both recorded when pulling ``entry/d`` - would be invisible
    if both collapsed to ``.``; the ``sub`` filter only restricts the range to the
    records the pull would touch."""
    blockers: list[str] = []  # rels of non-directory records with an open descendant range
    for entry in manifest.iter_manifest(manifest_path):
        if entry.path == ".":
            continue
        rel = entry.path.removeprefix("./")  # entry-relative
        if sub is not None and rel != sub and not rel.startswith(sub + "/"):
            continue  # outside the pulled range
        key = rel + "/" if entry.is_dir else rel
        while blockers:
            top = blockers[-1]
            if rel == top or rel.startswith(top + "/"):
                return top  # this record contradicts the non-directory record at `top`
            if key > top + "/":
                blockers.pop()  # past the blocker's descendant range (a sorted sibling)
                continue
            break
        if not entry.is_dir:
            blockers.append(rel)
    return None


def cmd_pull(cfg: Config, entry: str, opts: Opts, sub: str | None = None) -> int:
    entry_cfg = cfg.entries.get(entry)
    configured_path: str | None = entry_cfg["path"] if entry_cfg else None
    outpath = resolve_pull_destination(entry, configured_path, sub, opts.outpath)
    if outpath is None:
        console.err(f"no such entry in config: {entry}")
        console.err("use -o <path> to specify the output path")
        return 1

    # A sub-path restore to the configured location writes at
    # configured_path/sub; if an ancestor of sub is a symlink (or otherwise
    # inaccessible), the download would land outside the entry root - e.g.
    # `pull entry/dir/file` with a local `dir -> /outside` writes /outside/file.
    # (An -o destination is the user's own explicit path, so it is exempt; a
    # full-tree pull's root and recorded dirs are handled by the staging swap
    # and prepare_dir_conflicts.)
    if sub is not None and opts.outpath is None and configured_path is not None:
        ancestor_error = _reject_symlinked_sub_ancestors(configured_path, sub)
        if ancestor_error is not None:
            console.err(ancestor_error)
            return 1

    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        # 1. Fetch the manifest first; its content tells us file/dir
        #    without any extra head-object calls.
        if not download_manifest(cfg, entry, manifest_path, opts.verbose):
            if sub is not None:
                console.err(f"not found on S3: {entry}/{sub}")
            else:
                console.err(f"entry not found on S3: {entry}")
            return 1

        entry_is_dir = _entry_kind_from_manifest(manifest_path) == "dir"

        if sub is not None:
            if not entry_is_dir:
                console.err(f"sub path not allowed for single-file entry: {entry}")
                return 1
            kind = _sub_kind_from_manifest(manifest_path, sub)
            if kind == "missing":
                console.err(f"not found on S3: {entry}/{sub}")
                return 1
            is_dir = kind == "dir"
            has_data = kind in ("file", "dir")
        else:
            is_dir = entry_is_dir
            has_data = True

        # A manifest that records a non-directory (file/symlink) and a directory
        # or descendants at one logical path cannot be materialized: pulling it
        # restores some records and then the deferred symlink/dir replacement
        # rmtree's the subtree, destroying local data (incl. unrecorded files) on
        # a pull that then fails. Fail closed BEFORE any mutation; push --delete
        # prunes the stale records. (Only a directory range can hold the conflict.)
        if is_dir:
            conflict = _manifest_restore_conflict(manifest_path, sub)
            if conflict is not None:
                console.err(
                    f"{entry}: unrestorable manifest - a non-directory and a directory"
                    f" (or files under it) are both recorded at ./{conflict};"
                    f" run push --delete to prune the stale records"
                )
                return 1

        # 2. If everything in the manifest already matches local, both
        #    the s3 sync/cp and apply_manifest are no-ops. Skip them. Not
        #    under --checksum: this gate is the same size+mtime check whose
        #    blind spot --checksum exists to cover, so it must not stand
        #    between the user and the content comparison.
        window_ns = cfg.window_ns_for(entry)
        excludes: list[str] = entry_cfg.get("excludes", []) if entry_cfg else []
        ex = Excludes(excludes)
        # A named non-directory sub whose key is excluded is ignored in FULL
        # (docs/excludes.md): no download, no metadata, exit 0 - naming an
        # excluded path does not override the exclude. A directory sub is
        # not short-circuited: only its own record may be excluded while its
        # children restore normally (the lanes and the apply judge each key
        # alone).
        if sub is not None and not is_dir:
            if ex.excluded(sub, os.path.abspath(outpath).replace(os.sep, "/")):
                return 0
        # --checksum ignores this gate (see below), so on a real --checksum pull
        # skip the whole size+mtime walk under it - on a large tree that is
        # millions of wasted lstats before the content compare even starts. A
        # --checksum --dry-run still computes it: it is a preview (not the hot
        # path) and the dry-run metadata stand-in line below reads manifest_matches.
        manifest_matches = (opts.dryrun or not opts.checksum) and _manifest_matches_local(
            manifest_path, outpath, is_dir, sub, window_ns, ex
        )
        if manifest_matches and not opts.checksum:
            if opts.delete and is_dir:
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
        swap_done = False  # the old root was moved aside AND the new root swapped in
        replaced_root: str | None = None  # where the swapped-out old root now lives
        prep: list[tuple[str, int]] = []
        # True once something has already re-applied the recorded mode over
        # every prepped path (a clean apply_manifest does this itself, as an
        # ordinary part of the repair) - so the outer finally's restore below
        # is skipped there and only fires on a path that never got that far.
        prep_repaired = False
        if has_data and os.path.lexists(outpath):
            if is_dir:
                conflict = os.path.islink(outpath) or not os.path.isdir(outpath)
            else:
                conflict = not stat_mod.S_ISREG(os.lstat(outpath).st_mode)
            if conflict:
                if opts.dryrun:
                    console.out(f"(dry-run) would replace {outpath} (conflicting type)\n")
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
            if IS_WINDOWS and not opts.dryrun and stage_dir is None:
                prep = windows_collect_writable_prep(outpath, is_dir, manifest_path, sub)

            changed = False
            if has_data:
                # The compare only matters for the dir sync; a single-file transfer
                # always happens (we only reach it on a manifest mismatch). Its
                # size (from the manifest) routes a large file through multipart.
                dest = os.path.join(stage_dir, "new") if stage_dir is not None else outpath
                compare = sync_compare(cfg, opts, entry, manifest_path, sub=sub) if is_dir else None
                create: bool | FileFilter = True
                if is_dir and excludes:
                    create, compare = _pull_exclude_lanes(ex, sub, dest, compare)
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
                        create=create,
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
                    # The Windows writable prep is restored by the outer
                    # finally below, whichever way this function now returns.
                    return rc
                if not is_dir and not changed:
                    # The single-file lane downloaded nothing: its recorded
                    # object is gone (download_from_s3 warned - stale residue
                    # a push retires). Skip the record in FULL: no swap (the
                    # finally retires the empty stage, so a conflicting root
                    # stays as it is), and no metadata apply - stamping the
                    # record's mode/mtime onto content that was never
                    # restored would bless a local file this pull cannot
                    # vouch for, and erase the very drift that would surface
                    # the divergence later.
                    return 0
                if stage_dir is not None:
                    # The download is complete: swap in two atomic renames with
                    # the old root recoverable in between - the stage cleanup
                    # in the finally below then retires it (or, on a failed
                    # swap, the partial download).
                    replaced = os.path.join(stage_dir, "replaced")
                    # Record the destination BEFORE the rename: a SIGINT landing
                    # between the move and this assignment must still let the
                    # cleanup preserve the swapped-out old root, not delete it.
                    replaced_root = replaced
                    os.replace(outpath, replaced)
                    try:
                        os.replace(dest, outpath)
                        swap_done = True
                    except BaseException:
                        try:
                            os.replace(replaced, outpath)  # put the old root back
                            replaced_root = None  # old root restored: nothing to preserve
                        except OSError:
                            # The rollback itself failed: the old root stays at
                            # `replaced` (replaced_root still set), so the handler
                            # and finally below preserve it and report where.
                            pass
                        raise

            # 4. Apply manifest metadata (mode, mtime, symlinks): objectless or
            #    metadata-only diffs (empty dirs, symlinks, mode/mtime) have nothing
            #    to download yet still need applying. Only records whose local
            #    state differs from the record are touched. A downloaded file
            #    normally mismatches afterwards (the dir sync stamps the S3 upload
            #    time onto it, the file lane leaves the write time) and gets its
            #    recorded mtime back; a stamp landing inside the mtime window is a
            #    match and stays, like any other within-window drift. The gate also
            #    re-applies the recorded modes over the writable prep on success -
            #    the outer finally below covers every other case.
            if opts.dryrun:
                # One stand-in line for the metadata apply (mode / mtime /
                # symlinks), printed only when the real apply could repair
                # something: a stat-gate difference, or a planned transfer.
                if not manifest_matches or changed:
                    console.out(f"(dry-run) would apply manifest metadata: {outpath}\n")
                st = 0
            else:
                st = apply_manifest(
                    outpath,
                    is_dir,
                    manifest_path,
                    sub=sub,
                    window_ns=window_ns,
                    excludes=excludes,
                )
                if st == 0:
                    # A clean apply already re-chmod'd every prepped path to its
                    # recorded mode (the write bit prep added made it mismatch
                    # its record, so apply_manifest corrected it) - nothing left
                    # for the outer finally to do. A failure can leave one
                    # unrepaired (its own record's apply bailed out before
                    # reaching the chmod, e.g. a missing file or a failed
                    # symlink placement), so only then does it still apply.
                    prep_repaired = True

            if opts.delete and is_dir:
                # A stale record (warned about and skipped above) does not
                # gate this pass: its path has no local counterpart to
                # misjudge, and extras are judged by the manifest, which
                # still records it.
                if st != 0:
                    # The local tree is not in the recorded state; extras built
                    # on that view are not trustworthy deletion candidates.
                    console.err(f"{entry}: skipping --delete (the metadata apply failed)")
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

            # Any non-zero exit after a staged swap - the metadata apply OR the
            # --delete re-settle - means the pull did not fully succeed. Keep the
            # swapped-out old root instead of letting the cleanup destroy it: the
            # staging's promise that a failed pull does not cost the local state
            # it was replacing, held past the swap, not only across a failed
            # download. Checked after the --delete step so its failure counts too.
            if swap_done and st != 0 and replaced_root is not None:
                stage_holds_old_root = True
                console.err(
                    f"{entry}: pull failed after replacing {outpath};"
                    f" the previous {outpath} is preserved at {replaced_root}"
                )

            return st
        except BaseException:
            # An exception (S3 error, local I/O, SIGINT) skips the normal
            # return paths; the outer finally below still restores the Windows
            # writable prep, and runs before this propagates. If the old root
            # was moved into the stage (even mid-swap, before the assignment
            # completed), keep it - the finally's stage cleanup only removes a
            # stage that is NOT holding the stranded old root.
            if replaced_root is not None:
                stage_holds_old_root = True
                if os.path.lexists(replaced_root):
                    console.err(
                        f"{entry}: pull failed after replacing {outpath};"
                        f" the previous {outpath} is preserved at {replaced_root}"
                    )
            raise
        finally:
            # Put every prepped path back to its original mode on every exit
            # this function did not already resolve on its own (download
            # failure, a failed apply_manifest, an early
            # conflict-clearing return, or an exception) - never after a clean
            # apply, which already re-chmod'd them itself (prep_repaired,
            # above). This is the one place that restores the prep, so it can
            # never double up with another restore.
            if IS_WINDOWS and not prep_repaired:
                windows_restore_modes(prep)
            if stage_dir is not None:
                # Preserve the stage ONLY while it actually holds the stranded old
                # root (the swap did not cleanly retire or roll it back); on
                # success, rollback, or an interrupt before the old root moved in,
                # the stage holds nothing irreplaceable, so retire it.
                if not (
                    stage_holds_old_root and os.path.lexists(os.path.join(stage_dir, "replaced"))
                ):
                    shutil.rmtree(stage_dir, ignore_errors=True)
    except DeletionAbortedError:
        console.err(
            f"{entry}: aborted; the local tree was updated only as far as the answers went"
            " - pull again to finish it"
        )
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


def _collect_extra_aliases(
    manifest_path: str, outpath: str, sub: str | None, excludes: list[str]
) -> set[tuple[str, str]]:
    """The alias set ``remove_extras`` checks a candidate against: one
    ``(parent_rel, fs_alias_key(basename))`` pair per manifest-only record
    whose recorded spelling a name-folding filesystem (case, NFC/NFD, a
    Win32-trimmed trailing dot/space) could fold onto some local path - the
    W-F3 defense (pushing from POSIX and pulling on Windows or macOS splits
    one file into a manifest-only record plus a local-only extra with a
    different byte spelling; without this, the extras pass would delete the
    file the same pull just restored).

    Runs the SAME merge-join ``_delete_extras`` runs, a second time,
    read-only, purely to collect this set BEFORE the removal stream starts.
    This cannot be folded into ``apply_manifest``'s own merge-join instead:
    ``_mirror_extras`` (the caller two levels up) is invoked from two call
    sites in ``cmd_pull`` - one of them a no-op short-circuit where
    ``apply_manifest`` never runs at all (the "manifest already matches
    local" fast path); both paths must get alias protection uniformly, so
    collecting
    it has to be self-contained here, independent of whether or how apply
    ran.

    A manifest-only record only becomes an alias candidate when
    ``os.path.lexists`` finds something at its OWN recorded spelling - which
    a plain byte-different local walk would never do, but a name-folding
    filesystem's own path resolution does (case-insensitive lookup, NFC/NFD
    equivalence, or the Win32 trailing dot/space trim), the same fold that
    produced the manifest-only/local-only split in the first place. So the
    set stays empty on an ordinary tree, and its size is bounded by the
    number of records the filesystem actually folded - the same "bounded by
    actual conflicts" allowance ``apply_manifest``'s deferred-symlink list
    gets (see docs/overview.md). Cost is one extra lstat per manifest-only
    record, on top of the merge-join this already re-runs."""
    aliases: set[tuple[str, str]] = set()
    for _key, m, loc in manifest.merge_join(
        manifest_keyed(manifest_path, sub), local_keyed(outpath, excludes, sub)
    ):
        if m is None or loc is not None:
            continue
        rel, _m_entry = m
        if rel == ".":
            continue
        if os.path.lexists(os.path.join(outpath, rel)):
            parent = "." if "/" not in rel else rel.rsplit("/", 1)[0]
            name = rel.rsplit("/", 1)[-1]
            aliases.add((parent, fs_alias_key(name)))
    return aliases


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

    The local-only lane of the merge-join streams straight into
    ``remove_extras`` (never materialized as a list), which settles the
    post-order removal itself with an ancestor stack - memory bounded by the
    depth of directories currently open, not by how many extras exist.
    ``_collect_extra_aliases`` runs first (its own, separate pass over the
    same merge-join) so the W-F3 alias set is complete before any candidate
    is judged."""
    confirmer: DeleteConfirmer | None = None
    if not opts.dryrun:
        mode = resolve_answer_mode(yes=opts.yes)
        if mode is AnswerMode.ALL_NO:
            return 0, 0  # every answer is no: keep every extra, successfully
        if mode is AnswerMode.ASK:
            confirmer = DeleteConfirmer(mode, entry)

    aliases = _collect_extra_aliases(manifest_path, outpath, sub, excludes)

    def extras() -> Iterator[tuple[str, str, bool]]:
        for _key, m, loc in manifest.merge_join(
            manifest_keyed(manifest_path, sub), local_keyed(outpath, excludes, sub)
        ):
            if m is None and loc is not None:
                rel, st, _sym = loc
                if rel != ".":
                    yield rel, os.path.join(outpath, rel), stat_mod.S_ISDIR(st.st_mode)

    errors, removed = remove_extras(
        extras(), aliases=aliases, dryrun=opts.dryrun, confirm=confirmer
    )
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
    Skipped under --dry-run, which never applies metadata."""
    status, removed = _delete_extras(manifest_path, outpath, sub, excludes, opts=opts, entry=entry)
    if removed and not opts.dryrun:
        # warn_stale=False: the pull's first apply already warned about any
        # stale records; the re-settle must not repeat those warnings.
        settle = apply_manifest(
            outpath,
            True,
            manifest_path,
            sub=sub,
            window_ns=window_ns,
            excludes=excludes,
            warn_stale=False,
        )
        status = status or settle
    return status


def cmd_show(cfg: Config, entry: str, opts: Opts, file: str | None = None) -> int:
    if entry not in cfg.entries:
        console.err(f"no such entry: {entry}")
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
        console.err(f"no such entry: {entry}")
        return 1
    base_path: str = entry_cfg["path"]
    outpath = os.path.join(base_path, sub) if sub else base_path

    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        if not download_manifest(cfg, entry, manifest_path, opts.verbose):
            console.err(f"entry not found on S3: {entry}")
            return 1
        # Classify from the manifest (the record of the last push), not the
        # local filesystem: a directory entry whose local tree was deleted must
        # still map each record to its own child path (is_dir=False would fold
        # every record onto outpath and print duplicate/wrong lines).
        if sub is not None:
            sub_kind = _sub_kind_from_manifest(manifest_path, sub)
            if sub_kind == "missing":
                console.err(f"not found on S3: {entry}/{sub}")
                return 1
            is_dir = sub_kind == "dir"
        else:
            is_dir = _entry_kind_from_manifest(manifest_path) == "dir"
        excludes: list[str] = entry_cfg.get("excludes", [])
        use_color = _resolve_use_color(opts.color)
        window_ns = cfg.window_ns_for(entry)

        if not is_dir:
            # Single-file entry (or a file/symlink sub): one direct compare. A
            # leaf sub reached through a local symlinked ancestor is not cleanly
            # present at its recorded path - a full-entry status, walking
            # no-follow, would show it D - so report D rather than compare a file
            # the record does not describe (reached through the symlink).
            through_symlink = sub is not None and _symlinked_ancestor(base_path, sub)
            for entry_obj in manifest.iter_manifest(manifest_path):
                res = manifest_target(entry_obj, outpath, is_dir, sub)
                if res is None:
                    continue
                target, _rel = res
                if through_symlink:
                    console.out(f"D {target}\n")
                    continue
                # An inaccessible file (EACCES on an unsearchable parent) is not
                # missing: compare_to_local turns every OSError into st=None and
                # would silently report D. Distinguish it and warn instead.
                try:
                    os.lstat(target)
                except FileNotFoundError:
                    pass  # genuinely absent: check_metadata reports it D
                except OSError as e:
                    console.warn(f"warning: cannot access {target}: {e}")
                    continue
                block = check_metadata(
                    target,
                    entry_obj,
                    opts.verbose,
                    window_ns,
                    use_color=use_color,
                )
                if block:
                    console.out(block)
            return 0

        # A directory sub whose local root sits behind a symlinked ancestor
        # would make the walk compare an entry-outside tree; report every record
        # D (not cleanly present at its recorded path) and warn, like the leaf
        # sub and the full-entry no-follow walk do.
        if sub is not None and _symlinked_ancestor(base_path, sub):
            console.warn(
                f"warning: {entry}/{sub}: reached through a symlinked parent; not compared"
            )
            for _key, (rel, _entry_obj) in manifest_keyed(manifest_path, sub):
                if rel != ".":
                    console.out(f"D {os.path.join(outpath, rel)}\n")
            return 0

        # Directory tree: one streaming merge-join of the manifest against a
        # fresh walk decides everything - M (both sides, drifted), D
        # (manifest-only), A (local-only) - in key order, holding only the
        # current pair in memory. The walk's lstat/readlink feed the compare,
        # so no path is stat'd twice.
        for _key, m, loc in manifest.merge_join(
            manifest_keyed(manifest_path, sub),
            local_keyed(outpath, excludes, sub, warn=console.warn),
        ):
            if m is not None:
                rel, entry_obj = m
                target = outpath if rel == "." else os.path.join(outpath, rel)
                if loc is None:
                    console.out(f"D {target}\n")
                    continue
                _rel, st, sym = loc
                diff = compare_to_stat(
                    entry_obj,
                    st,
                    sym,
                    window_ns=window_ns,
                    use_color=use_color,
                )
                block = format_diff_block(diff, target, opts.verbose)
                if block:
                    console.out(block)
            elif loc is not None:
                rel, _st, _sym = loc
                if rel != ".":
                    console.out(f"A {os.path.join(outpath, rel)}\n")

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
    console.echo_command(opts.verbose, cmd)
    rc = subprocess.run(cmd).returncode
    # signal.SIGPIPE does not exist on Windows, and this check is evaluated
    # unconditionally regardless of rc, so guard on platform before touching it.
    if not IS_WINDOWS and rc == -signal.SIGPIPE:
        # The reader closed the pipe (e.g. `s3bak diff | head`): let run() map
        # this to the documented 141 instead of collapsing it to a plain 1.
        raise BrokenPipeError
    return rc


def _write_leaf_type_diff(label: str, backup: str, local: str) -> None:
    console.out(f"--- a/{label}\n+++ b/{label}\n-{backup}\n+{local}\n")


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
            console.err(f"not found on S3: {rel_key}")
            return 1
        try:
            local_mode = os.lstat(localfile).st_mode
        except FileNotFoundError:
            # The local file is missing: always a difference (exit 1), even
            # against a 0-byte backup - whose content diff vs /dev/null would
            # show nothing and (before this) exit 0, hiding the missing file.
            if os.path.getsize(tmppath) == 0:
                _write_leaf_type_diff(label, "regular file", "missing")
            else:
                _run_diff(tmppath, os.devnull, label, opts)
            return 1
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


def _ancestor_block_reason(root: str, rel: str) -> str | None:
    """Why ``root/rel``'s parent chain (``root`` itself plus every ancestor up to
    but excluding the final component) cannot be safely read through, or ``None``
    if it can:

    - ``"structural"`` - a symlink, a Windows directory junction, or another
      non-directory ancestor would redirect the read to a file the record does
      not describe (an entry-outside target in the worst case). This is a
      local type change, not a backup defect.
    - ``"inaccessible"`` - an ancestor could not be stat'd (EACCES/ELOOP), so
      reachability is undeterminable and the content simply cannot be read.

    A missing ancestor is not blocking (``None``): the caller reports the leaf as
    absent, not as unreachable. Read-only lstat probe."""
    acc = root
    to_check = [root]
    for part in rel.split("/")[:-1]:
        acc = os.path.join(acc, part)
        to_check.append(acc)
    for path in to_check:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError:
            return "inaccessible"
        # A junction lstats as an ordinary directory (Windows does not model
        # it as a symlink); without this check it would pass the S_ISDIR test
        # below as if it were a real directory.
        if not stat_mod.S_ISDIR(st.st_mode) or is_junction(st):
            return "structural"
    return None


def _symlinked_ancestor(root: str, rel: str) -> bool:
    """True if ``root/rel``'s parent chain cannot be safely read through - a
    symlink, a non-directory, or an inaccessible ancestor. Reading local content
    through such a path - verify --checksum's hash, diff's content compare, a
    single-file status/diff/verify stat - would touch a file the record does not
    describe. Callers that must tell an access error apart from a structural
    change (to warn rather than silently skip) use _ancestor_block_reason."""
    return _ancestor_block_reason(root, rel) is not None


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
    obj_path = os.path.join(tmpdir, "object")
    has_diff = 0

    try:
        assert cfg.store is not None

        # One streaming merge-join of the manifest against a fresh local walk -
        # the same shape cmd_status uses - so memory stays bounded by one
        # record on each side, not the whole manifest. The manifest, not every
        # object under the prefix, defines the backup: only its recorded
        # regular files are ever downloaded, one at a time (see below), so
        # orphan objects (from interrupted pushes) are ignored,
        # as pull/status do - and, crucially, conflicting orphans (a file and
        # a directory recorded at one path by out-of-band pushes) that a bulk
        # prefix sync could not even materialize onto one filesystem never
        # break the diff.
        #
        # warn=console.warn surfaces walk gaps (an unreadable directory hides
        # its children), so an otherwise-clean diff still warns (exit 2)
        # rather than hiding a local-only file that may sit unseen behind one.
        # The isdir/not-islink gate matches the old local-only walk: a
        # missing, non-directory, or symlinked outpath yields no local items
        # at all (a symlinked outpath is still caught per-record below, via
        # _symlinked_ancestor - which checks the root itself too).
        for _key, m, loc in manifest.merge_join(
            manifest_keyed(manifest_path, sub),
            local_keyed(outpath, excludes, sub, warn=console.warn)
            if os.path.isdir(outpath) and not os.path.islink(outpath)
            else (),
        ):
            if m is not None:
                rel, record = m
                if rel == "." or record.is_dir:
                    continue
                local = os.path.join(outpath, rel)

                if record.is_file and record.sym_target is None:
                    if _symlinked_ancestor(outpath, rel):
                        # Never diff a file reached through a symlinked parent:
                        # it would disclose an entry-outside target's contents
                        # (the final-component guard below does the same for a
                        # leaf symlink).
                        _write_leaf_type_diff(
                            rel, "regular file", "unreachable (through a symlinked parent)"
                        )
                        has_diff = 1
                        continue
                    # A fixed destination name, removed in the finally right
                    # after this record's compare: at most one backup object
                    # sits on disk at a time, however many the manifest records.
                    try:
                        if not cfg.store.get_object(
                            f"{rel_prefix}/{rel}",
                            obj_path,
                            size=record.size,
                            verbose=opts.verbose,
                        ):
                            console.err(f"expected backup object missing: {rel_prefix}/{rel}")
                            has_diff = 1
                            continue
                        local_mode: int | None
                        if loc is not None:
                            _lrel, st, _sym = loc
                            local_mode = st.st_mode
                        else:
                            # The walk filters excludes, and cannot pair a
                            # manifest file record (sort key "x") with a
                            # same-named local directory (sort key "x/") - a
                            # type change, not a hidden path, but the same
                            # unpaired shape. Either way "not walked" may mean
                            # hidden rather than missing: judge from a direct
                            # lstat, like apply_manifest's own fallback.
                            try:
                                local_mode = os.lstat(local).st_mode
                            except FileNotFoundError:
                                local_mode = None
                        if local_mode is None:
                            _run_diff(obj_path, os.devnull, rel, opts)
                            has_diff = 1
                        elif not stat_mod.S_ISREG(local_mode):
                            _write_leaf_type_diff(
                                rel,
                                "regular file",
                                _local_leaf_description(local, local_mode),
                            )
                            has_diff = 1
                        elif _run_diff(obj_path, local, rel, opts) != 0:
                            has_diff = 1
                    finally:
                        try:
                            os.unlink(obj_path)
                        except FileNotFoundError:
                            pass

                elif record.sym_target is not None:
                    if _symlinked_ancestor(outpath, rel):
                        _write_leaf_type_diff(
                            rel,
                            f"symlink -> {record.sym_target!r}",
                            "unreachable (through a symlinked parent)",
                        )
                        has_diff = 1
                        continue
                    if loc is not None:
                        _lrel, st, sym = loc
                        local_value = _local_leaf_description(local, st.st_mode)
                        if stat_mod.S_ISLNK(st.st_mode) and sym == record.sym_target:
                            continue
                    else:
                        # The walk filters excludes, and cannot pair a manifest
                        # symlink record (sort key "x") with a same-named local
                        # directory (sort key "x/") - a type change, not a
                        # hidden path, but the same unpaired shape. Either way
                        # "not walked" may mean hidden rather than missing:
                        # judge from a direct lstat, like apply_manifest's own
                        # fallback.
                        try:
                            local_mode = os.lstat(local).st_mode
                        except FileNotFoundError:
                            local_value = "missing"
                        else:
                            local_value = _local_leaf_description(local, local_mode)
                            if (
                                stat_mod.S_ISLNK(local_mode)
                                and os.readlink(local) == record.sym_target
                            ):
                                continue
                    _write_leaf_type_diff(rel, f"symlink -> {record.sym_target!r}", local_value)
                    has_diff = 1

                else:  # special file (neither directory, regular file, nor symlink)
                    if _symlinked_ancestor(outpath, rel):
                        _write_leaf_type_diff(
                            rel, "special file", "unreachable (through a symlinked parent)"
                        )
                        has_diff = 1
                        continue
                    if loc is not None:
                        _lrel, st, _sym = loc
                        if stat_mod.S_IFMT(st.st_mode) == stat_mod.S_IFMT(record.mode):
                            continue
                        local_value = _local_leaf_description(local, st.st_mode)
                    else:
                        # The walk filters excludes, and cannot pair a manifest
                        # special-file record (sort key "x") with a same-named
                        # local directory (sort key "x/") - a type change, not
                        # a hidden path, but the same unpaired shape. Either
                        # way "not walked" may mean hidden rather than
                        # missing: judge from a direct lstat, like
                        # apply_manifest's own fallback.
                        try:
                            local_mode = os.lstat(local).st_mode
                        except FileNotFoundError:
                            local_value = "missing"
                        else:
                            if stat_mod.S_IFMT(local_mode) == stat_mod.S_IFMT(record.mode):
                                continue
                            local_value = _local_leaf_description(local, local_mode)
                    _write_leaf_type_diff(rel, "special file", local_value)
                    has_diff = 1

            elif loc is not None:
                rel, st, _sym = loc
                if rel == "." or stat_mod.S_ISDIR(st.st_mode):
                    continue
                local = os.path.join(outpath, rel)
                if stat_mod.S_ISREG(st.st_mode):
                    _run_diff(os.devnull, local, rel, opts)
                else:
                    _write_leaf_type_diff(
                        rel, "missing", _local_leaf_description(local, st.st_mode)
                    )
                has_diff = 1

        return has_diff
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def cmd_diff(cfg: Config, entry: str, opts: Opts, file: str | None = None) -> int:
    entry_cfg = cfg.entries.get(entry)
    if not entry_cfg:
        console.err(f"no such entry: {entry}")
        return 1
    outpath: str = entry_cfg["path"]

    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        if not download_manifest(cfg, entry, manifest_path, opts.verbose):
            console.err(f"entry not found on S3: {entry}")
            return 1
        entry_is_dir = _entry_kind_from_manifest(manifest_path) == "dir"

        if file:
            if not entry_is_dir:
                console.err(f"sub path not allowed for single-file entry: {entry}")
                return 1
            file = file.removeprefix("./")
            kind = _sub_kind_from_manifest(manifest_path, file)
            if kind == "missing":
                console.err(f"not found on S3: {entry}/{file}")
                return 1
            local = os.path.join(outpath, *file.split("/"))
            if _symlinked_ancestor(outpath, file):
                # A sub reached through a local symlinked ancestor - including one
                # ABOVE a directory sub's own root - must never read/compare the
                # entry-outside target (content, link, or type), whatever the
                # recorded kind. Report it unreachable. (diff_backup's own guards
                # only cover ancestors at/under the sub root, not above it.)
                backup_desc = {"dir": "directory", "symlink": "symlink", "special": "special file"}
                _write_leaf_type_diff(
                    f"{entry}/{file}",
                    backup_desc.get(kind, "regular file"),
                    "unreachable (through a symlinked parent)",
                )
                return 1
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
        console.err(f"{self.entry}: {msg}")
        self.errors += 1

    def warn(self, msg: str) -> None:
        console.warn(f"warning: {self.entry}: {msg}")
        self.warnings += 1

    def pending(self, msg: str) -> None:
        console.out(f"{self.entry}: pending change: {msg}\n")
        self.pendings += 1

    def finish(self) -> int:
        """Print the per-entry summary line - the record/object tallies double
        as a heartbeat for cron logs - and return the entry's exit status."""
        counts = f"{self.file_records} file record(s), {self.objects} data object(s)"
        if self.pendings:
            counts += f", {self.pendings} pending change(s)"
        if self.errors or self.warnings:
            console.out(
                f"{self.entry}: {self.errors} error(s), {self.warnings} warning(s) ({counts})\n"
            )
        else:
            console.out(f"{self.entry}: OK ({counts})\n")
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
        from boto3_s3 import Boto3S3Error

        self._differs = cfg.store.etag_checker()
        # Hashing the local file reads it: a vanished file raises OSError, an
        # unreadable one AccessDeniedError (a Boto3S3Error) from the reconstruct
        # open. Both mean "could not check", not "matches" - and must not crash
        # the whole verify. (The comparison is UPLOAD-shaped and reads only the
        # local side, so a Boto3S3Error here is always a local-read failure.)
        self._read_errors: tuple[type[BaseException], ...] = (OSError, Boto3S3Error)
        self._window_ns = cfg.window_ns_for(entry)
        self._report = report
        self._size = cfg.store.compare_pool_size()
        self._pool: ThreadPoolExecutor | None = None
        self._queue: deque[tuple[Future[bool | None], ManifestEntry, os.stat_result, str]] = deque()

    def check(self, rel_key: str, local_path: str, record: ManifestEntry, obj: ObjectMeta) -> None:
        try:
            st = os.lstat(local_path)
        except FileNotFoundError:
            return  # a kept deletion has no local counterpart: nothing to compare
        except OSError as e:
            # An unsearchable parent etc. is not "nothing to compare": a
            # verification tool must say it could not check, not report OK.
            self._report.warn(f"cannot read local file for --checksum: {local_path} ({e})")
            return
        if not stat_mod.S_ISREG(st.st_mode):
            return  # a local type change is a status finding, not a backup defect

        def hash_one() -> bool | None:
            try:
                return self._differs(rel_key, local_path, obj.size, obj.etag)
            except self._read_errors:
                return None  # unreadable/vanished mid-check: reported as a warning below

        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=self._size, thread_name_prefix="s3bak-verify"
            )
        self._queue.append((self._pool.submit(hash_one), record, st, local_path))
        self._drain(self._size * 2)

    def _drain(self, limit: int) -> None:
        while len(self._queue) > limit:
            future, record, st, local_path = self._queue.popleft()
            differs = future.result()
            if differs is None:
                # Could not read the file to hash it: warn rather than let a
                # silently-skipped file read as a passing content check.
                self._report.warn(f"cannot read local file for --checksum: {local_path}")
                continue
            if not differs:
                continue  # content matches the stored object
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


def _check_archived(report: _VerifyReport, url: str, obj: ObjectMeta) -> None:
    """Advise that an object sits in an archive storage tier - not a defect in
    the backup, just a fact about where it currently lives. Applies to every
    listed object - pull's listing-driven sync fetches unrecorded objects
    too."""
    if obj.storage_class in _ARCHIVED_CLASSES:
        report.warn(
            f"archived storage class {obj.storage_class}: {url}"
            f" (a pull cannot fetch it until RestoreObject completes)"
        )


def _report_restore_conflict(report: _VerifyReport, manifest_path: str, sub: str | None) -> None:
    """Flag a manifest that records a non-directory and a directory/descendants
    at one logical path - unrestorable (pull fails closed on it, see
    _manifest_restore_conflict)."""
    conflict = _manifest_restore_conflict(manifest_path, sub)
    if conflict is not None:
        report.error(
            f"unrestorable: a non-directory and a directory (or files under it) are both"
            f" recorded at ./{conflict} (pull cannot restore it; push --delete prunes it)"
        )


def _verify_dir(
    cfg: Config,
    entry: str,
    report: _VerifyReport,
    manifest_path: str,
    sub: str | None,
    opts: Opts,
    local_base: str,
    content_reachable: bool = True,
) -> None:
    """Merge-join the manifest records against the S3 listing - both ascend in
    key byte order, so one streaming pass checks the whole correspondence:
    every file record has its object (size intact, class restorable), every
    non-file record has none, and every object is accounted for.

    ``content_reachable`` False (a directory sub whose local root sits behind a
    symlinked ancestor) skips the --checksum content hash, which would otherwise
    read entry-outside files. The S3<->manifest checks still run (no local
    access needed)."""
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

    checker = _ContentChecker(cfg, entry, report) if opts.checksum and content_reachable else None
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
            if obj is not None:
                report.objects += 1
                _check_archived(report, f"{cfg.prefix}/{rel_base}/{obj.key}", obj)
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
                    elif checker is not None:
                        reason = _ancestor_block_reason(local_base, key)
                        if reason == "inaccessible":
                            # An unreadable ancestor: the content cannot be hashed,
                            # so warn (rc 2) rather than silently pass as OK.
                            report.warn(
                                "cannot read local file for --checksum: "
                                f"{os.path.join(local_base, *key.split('/'))}"
                            )
                        elif reason is None:
                            local_path = os.path.join(local_base, *key.split("/"))
                            checker.check(f"{rel_base}/{key}", local_path, record, obj)
                        # reason == "structural": a symlinked/non-directory ancestor
                        # redirects to a file the record does not describe (a local
                        # type change, not a backup defect): skip, like a type change.
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
    content_reachable: bool = True,
) -> None:
    """Verify one recorded regular file (a single-file entry, or a file
    sub-path) against its exact object - a head probe, since a lone file has
    no listing to stream. ``content_reachable`` False (a file sub-path reached
    through a local symlinked ancestor) skips the --checksum content hash, which
    would otherwise read a file the record does not describe."""
    assert cfg.store is not None
    report.file_records += 1
    head = cfg.store.head_object(rel_key, verbose=opts.verbose)
    if head is None:
        report.error(f"missing data object: {cfg.prefix}/{rel_key} (pull cannot restore it)")
        return
    report.objects += 1
    _check_archived(report, f"{cfg.prefix}/{rel_key}", head)
    if record.size != head.size:
        report.error(
            f"size mismatch: {cfg.prefix}/{rel_key} (manifest {record.size}, S3 {head.size})"
        )
        return
    if opts.checksum and content_reachable:
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
        console.err(f"no such entry: {entry}")
        return 1
    base_path: str = entry_cfg["path"]
    report = _VerifyReport(entry)
    assert cfg.store is not None

    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        if not download_manifest(cfg, entry, manifest_path, opts.verbose):
            # No manifest: an unrecorded backup (an interrupted push)
            # and no backup at all are different emergencies - tell them apart.
            # Count the stray objects (streaming) so the summary's object tally
            # reflects what was actually found instead of a misleading 0.
            report.objects = 1 if cfg.store.head_object(entry, verbose=opts.verbose) else 0
            report.objects += sum(1 for _ in cfg.store.iter_objects(entry, verbose=opts.verbose))
            if report.objects:
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
                console.err(f"sub path not allowed for single-file entry: {entry}")
                return 1
            kind = _sub_kind_from_manifest(manifest_path, sub)
            if kind == "missing":
                console.err(f"not found on S3: {entry}/{sub}")
                return 1
            rel_key = f"{entry}/{sub}"
            local_path = os.path.join(base_path, *sub.split("/"))
            # An unreadable ancestor of the sub root means --checksum cannot read
            # any local content: warn (rc 2) rather than silently report OK. A
            # structural (symlinked/non-dir) ancestor is a local type change and
            # is skipped silently. Either way the content compare is off.
            sub_reason = _ancestor_block_reason(base_path, sub)
            if opts.checksum and sub_reason == "inaccessible":
                report.warn(f"cannot read local files for --checksum through: {local_path}")
            content_reachable = sub_reason is None
            if kind == "dir":
                _report_restore_conflict(report, manifest_path, sub)
                _verify_dir(
                    cfg,
                    entry,
                    report,
                    manifest_path,
                    sub,
                    opts,
                    local_path,
                    content_reachable=content_reachable,
                )
            elif kind == "file":
                record = next(
                    r
                    for r in manifest.iter_manifest(manifest_path)
                    if r.path.removeprefix("./") == sub
                )
                _verify_file_record(
                    cfg,
                    entry,
                    report,
                    record,
                    rel_key,
                    local_path,
                    opts,
                    content_reachable=content_reachable,
                )
            else:
                kind_name = "symlink" if kind == "symlink" else "special file"
                _verify_objectless_record(cfg, report, rel_key, kind_name, opts)
        elif entry_is_dir:
            _report_restore_conflict(report, manifest_path, None)
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
        console.warn(
            f"warning: stale manifest (no configured entry):"
            f" {cfg.prefix}/{manifest.manifest_key(name)}"
        )
    for name in sorted(objects):
        # A name its own manifest accounts for is covered: configured, by the
        # per-entry verify; unconfigured, by the stale-manifest warning above.
        if name.endswith(manifest.MANIFEST_SUFFIX) or name in cfg.entries or name in manifests:
            continue
        console.warn(f"warning: top-level object outside any configured entry: {cfg.prefix}/{name}")
    for name in sorted(set(prefixes)):
        if name in cfg.entries or name in manifests:
            continue
        console.warn(
            f"warning: data tree without a manifest or configured entry: {cfg.prefix}/{name}/"
        )


def cmd_list(cfg: Config, opts: Opts) -> int:
    for key in sorted(cfg.entries.keys()):
        path = cfg.entries[key]["path"]
        console.out(f"{key:<20s} {path}\n")
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
        console.out(
            f"{format(entry.mode, 'o'):<6s} {entry.owner:<8s} {entry.group:<8s} "
            f"{size:>8s}  {when}  {display}\n"
        )


def cmd_ls_remote(cfg: Config, opts: Opts, entry: str | None = None, sub: str | None = None) -> int:
    assert cfg.store is not None
    if entry is None:
        names, _prefixes = cfg.store.list_top_level(verbose=opts.verbose)
        for name in names:
            if name.endswith(manifest.MANIFEST_SUFFIX):
                console.out(f"{name.removesuffix(manifest.MANIFEST_SUFFIX)}\n")
        return 0

    if entry not in cfg.entries:
        console.err(f"no such entry: {entry}")
        return 1

    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        if not download_manifest(cfg, entry, manifest_path, opts.verbose):
            console.err(f"entry not found on S3: {entry}")
            return 1
        if sub is not None and _sub_kind_from_manifest(manifest_path, sub) == "missing":
            console.err(f"not found on S3: {entry}/{sub}")
            return 1
        show_entry_files(manifest_path, sub=sub)
        return 0
    finally:
        os.unlink(manifest_path)
