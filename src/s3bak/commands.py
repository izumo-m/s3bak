# Requires Python 3.10+
"""The command layer: one ``cmd_*`` per subcommand plus their private helpers.

Orchestrates the lower layers - store (S3), syncops (manifest<->S3),
restore (local filesystem), compare (status/diff) - into the push / pull /
status / diff / show / list / ls-remote behaviours. ``cli.py`` parses argv and
dispatches here.
"""

from __future__ import annotations

import os
import shutil
import stat as stat_mod
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from s3bak import localwalk, manifest
from s3bak.compare import (
    _diff_color_flag,
    _fmt_mtime,
    _resolve_use_color,
    check_metadata,
    compare_to_local,
    compare_to_stat,
    format_diff_block,
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
    write_output,
    write_stderr,
)
from s3bak.manifest import ManifestEntry
from s3bak.restore import (
    apply_manifest,
    manifest_target,
    remove_extras,
    resolve_manifest_rel,
    resolve_pull_destination,
    windows_collect_writable_prep,
    windows_restore_modes,
)
from s3bak.syncops import (
    download_from_s3,
    download_manifest,
    patch_manifest_subtree,
    sync_compare,
    write_manifest_to_aws,
)


def _run_hook(name: str, hook: list[str] | None, opts: Opts) -> int:
    """Run one configured hook directly, without a command shell."""
    if not hook:
        return 0
    if opts.dryrun:
        print(f"(dry-run) would run {name}: {hook!r}")
        return 0
    if opts.verbose:
        write_stderr(f"+ {name}: {hook!r}\n")
    rc = subprocess.run(hook, shell=False).returncode
    if rc != 0:
        err(f"{name} failed (exit {rc}): {hook!r}")
    return rc


def upload_manifest(
    cfg: Config,
    entry: str,
    target: str,
    excludes: list[str],
    opts: Opts,
    *,
    old_manifest: str | None = None,
    keep_old: manifest.KeepOld = False,
) -> int:
    """Write the manifest to S3, then run the entry's post_hook."""
    post_hook: list[str] | None = cfg.entries[entry].get("post_hook")

    if opts.dryrun:
        print(f"(dry-run) would update manifest: {manifest.manifest_key(entry)}")
        return _run_hook("post_hook", post_hook, opts)

    write_manifest_to_aws(
        cfg, entry, target, excludes, opts.verbose, old_manifest=old_manifest, keep_old=keep_old
    )

    return _run_hook("post_hook", post_hook, opts)


@dataclass
class _PushDeletePlan:
    """How this push treats S3 orphans: the sync's delete-lane value, plus what
    the manifest merge must do about old-only records afterwards."""

    lane: Any  # sync_up delete=: False | True | per-orphan callable
    confirmer: DeleteConfirmer | None  # interactive mode only
    mirror: bool  # --delete with every answer yes: the manifest is a pure walk
    _kept: manifest.KeptKeys | None = None

    def keep_old(self) -> manifest.KeepOld:
        """The manifest merge policy. Call after the sync: an interactive run's
        kept-keys file is complete only once every orphan was decided."""
        if self.mirror:
            return False
        if self.confirmer is not None:
            path = self.confirmer.kept_keys_path()
            if path is None:  # every answer deleted its object
                return False
            self._kept = manifest.KeptKeys(path)
            return self._kept
        return True  # no deletions were made: keep every old-only record

    def close(self) -> None:
        if self._kept is not None:
            self._kept.close()
            self._kept = None
        if self.confirmer is not None:
            self.confirmer.close()


def _plan_push_deletes(cfg: Config, entry: str, sub: str | None, opts: Opts) -> _PushDeletePlan:
    if not opts.delete:
        return _PushDeletePlan(lane=False, confirmer=None, mirror=False)
    if opts.dryrun:
        # Report every candidate. The lane must be the plain True: the library
        # invokes a callable under dryrun too, and a dry run never prompts.
        return _PushDeletePlan(lane=True, confirmer=None, mirror=False)
    mode = resolve_answer_mode(yes=opts.yes)
    if mode is AnswerMode.ALL_YES:
        return _PushDeletePlan(lane=True, confirmer=None, mirror=True)
    if mode is AnswerMode.ALL_NO:
        return _PushDeletePlan(lane=False, confirmer=None, mirror=False)
    confirmer = DeleteConfirmer(mode, entry)

    def decide(info: Any) -> bool:
        # compare_key is relative to the sync's S3 listing prefix, i.e. to the
        # sub on a sub-path push; the kept key must be entry-rooted to match
        # the manifest merge.
        rel = info.compare_key if sub is None else f"{sub}/{info.compare_key}"
        return confirmer.confirm(f"{cfg.prefix}/{entry}/{rel}", kept_key=rel)

    return _PushDeletePlan(lane=decide, confirmer=confirmer, mirror=False)


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

    if not os.path.lexists(local_sub):
        if not opts.delete:
            err(f"local path does not exist (use --delete to remove its backup): {local_sub}")
            return 1
        # --delete cannot combine with --meta-only/--data-only (rejected at the
        # CLI), so this is always the full deletion: one confirmation covers
        # the subtree's objects and manifest records together.
        if not opts.dryrun and not confirm_subtree_delete(
            resolve_answer_mode(yes=opts.yes), entry, s3_sub_path
        ):
            err(f"backup subtree not deleted (answer y, or use --yes): {s3_sub_path}")
            return 1
        assert cfg.store is not None
        result = cfg.store.delete_subtree(sub_rel, dryrun=opts.dryrun, verbose=opts.verbose)
        if result.stdout:
            write_output(f"{result.stdout}\n")
        if result.returncode != 0:
            if result.stderr:
                write_stderr(f"{result.stderr}\n")
            return result.returncode
        did_work = bool(result.stdout)
        did_work = patch_manifest_subtree(cfg, entry, target_root, sub, excludes, opts) or did_work
        return _run_hook("post_hook", post_hook, opts) if did_work else 0

    if opts.meta_only:
        # --meta-only --delete is rejected at the CLI: always the keep policy.
        did_work = patch_manifest_subtree(
            cfg, entry, target_root, sub, excludes, opts, keep_old=True
        )
        return _run_hook("post_hook", post_hook, opts) if did_work else 0

    assert cfg.store is not None
    st = os.lstat(local_sub)
    did_work = False

    plan = _plan_push_deletes(cfg, entry, sub, opts)
    try:
        if stat_mod.S_ISLNK(st.st_mode):
            # symlink: upload nothing, just update manifest line.
            pass
        elif os.path.isdir(local_sub):
            fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
            os.close(fd)
            compare = None
            try:
                # --checksum ignores the manifest entirely; skip the download.
                have_manifest = not opts.checksum and download_manifest(
                    cfg, entry, manifest_path, opts.verbose
                )
                compare = sync_compare(
                    cfg, opts, entry, manifest_path if have_manifest else None, sub=sub
                )
                result = cfg.store.sync_up(
                    local_sub,
                    sub_rel,
                    file_filter=manifest.exclude_filter(excludes, sub=sub) if excludes else None,
                    compare=compare,
                    delete=plan.lane,
                    dryrun=opts.dryrun,
                    verbose=opts.verbose,
                )
            finally:
                # The streaming ManifestFilter holds the temp manifest open; close it
                # before unlink (an open file cannot be removed on Windows).
                if isinstance(compare, manifest.ManifestFilter):
                    compare.close()
                os.unlink(manifest_path)
            if result.returncode != 0:
                write_output(result.stdout)
                if result.stderr:
                    write_stderr(result.stderr)
                return result.returncode
            if result.stdout:
                write_output(f"{result.stdout}\n")
                did_work = True
        elif stat_mod.S_ISREG(st.st_mode):
            # Regular file: an explicit sub-path push always uploads. There is
            # no S3 listing here, so --delete cannot offer orphans under a
            # same-named former directory; their records still fall out of the
            # manifest patch below (the objects stay until a directory push).
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
        else:
            err(f"sub path must be a regular file, directory, or symlink: {local_sub}")
            return 1

        if not opts.data_only:
            did_work = (
                patch_manifest_subtree(
                    cfg, entry, target_root, sub, excludes, opts, keep_old=plan.keep_old()
                )
                or did_work
            )
        return _run_hook("post_hook", post_hook, opts) if did_work else 0
    finally:
        plan.close()


def _single_file_needs_upload(cfg: Config, entry: str, target: str, opts: Opts) -> bool:
    """The single-file counterpart of the sync compare: size+mtime check against
    the entry's one-record manifest (or EtagComparison under --checksum).

    Upload unless the manifest holds a regular-file record for exactly this
    basename (a stale dir-shaped manifest, e.g. from an entry that used to be
    a directory, must not suppress the upload), the local stat matches it, AND
    the data object actually exists on S3 - a `--meta-only` push or an S3-side
    delete leaves a manifest with no object behind it, which only this
    head-object probe can see (a dir entry self-heals via the sync listing;
    a single file has no listing)."""
    assert cfg.store is not None
    if opts.checksum:
        return cfg.store.needs_upload(entry, target, verbose=opts.verbose)
    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        if not download_manifest(cfg, entry, manifest_path, opts.verbose):
            return True
        st = os.lstat(target)
        basename = os.path.basename(target)
        for m in manifest.iter_manifest(manifest_path):
            if m.path == basename and m.sym_target is None and m.is_file:
                if not m.matches_stat(st, cfg.window_ns_for(entry)):
                    return True
                return cfg.store.head_object(entry, verbose=opts.verbose) is None
        return True
    finally:
        os.unlink(manifest_path)


def _single_file_manifest_matches(cfg: Config, entry: str, target: str, opts: Opts) -> bool:
    """Whether a valid manifest already describes this single-file entry."""
    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        if not download_manifest(cfg, entry, manifest_path, opts.verbose):
            return False
        record = next(manifest.iter_manifest(manifest_path))
        return record.path == os.path.basename(target)
    finally:
        os.unlink(manifest_path)


def _manifest_structure_matches_local(
    manifest_path: str, target: str, excludes: list[str], *, ignore_manifest_only: bool
) -> bool:
    """Compare manifest-visible tree structure without treating metadata drift
    as a reason to rewrite an existing manifest.

    Data transfer output normally drives refresh, but empty directories and
    symlinks have no S3 object. Their add/remove/type/target changes must still
    make an ordinary push rewrite the manifest. Under the default keep policy
    (`ignore_manifest_only`), manifest-only records are the expected shape of a
    kept deletion, not a change; under --delete they demand a rewrite that
    settles them. The merge remains streaming.
    """
    manifest_items = (
        (manifest.entry_sort_key(entry.path, entry.is_dir), entry)
        for entry in manifest.iter_manifest(manifest_path)
    )
    local_items = (
        (manifest.entry_sort_key(path, stat_mod.S_ISDIR(st.st_mode)), (st, sym))
        for path, st, sym in localwalk.walk_tree(target, excludes)
    )
    matches = True
    for _key, record, local in manifest.merge_join(manifest_items, local_items):
        if local is None:
            if not ignore_manifest_only:
                matches = False
            continue
        if record is None:
            matches = False
            continue
        st, sym = local
        if stat_mod.S_IFMT(record.mode) != stat_mod.S_IFMT(st.st_mode):
            matches = False
        if record.sym_target != sym:
            matches = False
    return matches


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

    # --meta-only refreshes the manifest and runs the post_hook even with no data
    # change: the supported way to re-run the post_hook on demand (intended).
    # A directory refresh merges against the old manifest with every old-only
    # record kept: --meta-only moves no data, so it must not drop the records
    # of objects that are still on S3 (--meta-only --delete is rejected).
    if opts.meta_only:
        if not os.path.isdir(target):
            return upload_manifest(cfg, entry, target, excludes, opts)
        fd, old_path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            have_old = not opts.dryrun and download_manifest(cfg, entry, old_path, opts.verbose)
            return upload_manifest(
                cfg,
                entry,
                target,
                excludes,
                opts,
                old_manifest=old_path if have_old else None,
                keep_old=True,
            )
        finally:
            os.unlink(old_path)

    results = ""
    refresh_manifest = False
    assert cfg.store is not None

    manifest_path: str | None = None
    have_manifest = False
    plan = _plan_push_deletes(cfg, entry, None, opts)
    try:
        if os.path.isdir(target):
            fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
            os.close(fd)
            compare = None
            structure_changed = False
            try:
                # A checksum compare does not use manifest file stats, but an
                # ordinary push still needs the manifest to notice objectless tree
                # changes (empty directories and symlinks) and validate its source
                # of truth. --data-only is the one mode that can skip it.
                have_manifest = not (opts.checksum and opts.data_only) and download_manifest(
                    cfg, entry, manifest_path, opts.verbose
                )
                compare = sync_compare(cfg, opts, entry, manifest_path if have_manifest else None)
                result = cfg.store.sync_up(
                    target,
                    entry,
                    file_filter=manifest.exclude_filter(excludes) if excludes else None,
                    compare=compare,
                    delete=plan.lane,
                    dryrun=opts.dryrun,
                    verbose=opts.verbose,
                )
                if (
                    result.returncode == 0
                    and not result.stdout
                    and not opts.data_only
                    and have_manifest
                ):
                    structure_changed = not _manifest_structure_matches_local(
                        manifest_path, target, excludes, ignore_manifest_only=not opts.delete
                    )
            finally:
                # The streaming ManifestFilter holds the temp manifest open; close
                # it early (an open file cannot be removed on Windows). The temp
                # manifest itself lives on: the merge reads it as the old side.
                if isinstance(compare, manifest.ManifestFilter):
                    compare.close()
            if result.returncode != 0:
                write_output(result.stdout)
                if result.stderr:
                    write_stderr(result.stderr)
                return result.returncode
            results = result.stdout
            refresh_manifest = bool(results)
            if not refresh_manifest and not opts.data_only:
                refresh_manifest = not have_manifest or structure_changed
        elif _single_file_needs_upload(cfg, entry, target, opts):
            # Single-file entry that fails the size+mtime check against its manifest
            # (or the --checksum ETag comparison), or was never pushed: upload it.
            if opts.dryrun:
                # Set results only; the shared writer below emits it (and the truthy
                # results drives the dryrun manifest line). Printing here too would
                # double the line.
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

        if (
            opts.checksum
            and not refresh_manifest
            and not opts.data_only
            and not os.path.isdir(target)
        ):
            # ETag equality can skip an already-present data object even when its
            # manifest was deleted or still names an older configured basename.
            refresh_manifest = not _single_file_manifest_matches(cfg, entry, target, opts)

        if results:
            write_output(f"{results}\n")

        # Refresh after a data transfer or deletion, an objectless structural
        # change, or the first push even when an empty tree produced no transfer
        # lines. The default compare is the manifest size+mtime check (mtime
        # within the window), so a mode-only change or an mtime drift inside the
        # window does not refresh an existing manifest; `status` keeps showing
        # that diff until `push --meta-only`. Owner/group are informational and
        # not comparison inputs. Deliberate spec choice. Note
        # --meta-only asserts "S3 matches local" without making it true: any
        # never-pushed local edit becomes invisible to the size+mtime check
        # afterwards, so it is a metadata refresh, never a substitute for a real
        # push.
        if refresh_manifest and not opts.data_only:
            st = upload_manifest(
                cfg,
                entry,
                target,
                excludes,
                opts,
                old_manifest=manifest_path if have_manifest else None,
                keep_old=plan.keep_old(),
            )
            if st != 0:
                return st

        if results and opts.data_only:
            post_hook: list[str] | None = entry_cfg.get("post_hook")
            return _run_hook("post_hook", post_hook, opts)

        return 0
    except DeletionAbortedError:
        # q mid-confirmation: already-confirmed deletions may have run, but the
        # manifest was not rewritten and no hook fires. The next push --delete
        # settles any records those deletions left behind.
        err(f"{entry}: aborted")
        return 1
    finally:
        plan.close()
        if manifest_path is not None:
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
        manifest_matches = _manifest_matches_local(
            manifest_path, outpath, is_dir, sub, cfg.window_ns_for(entry)
        )
        if manifest_matches and not opts.checksum:
            if not opts.meta_only and opts.delete and is_dir:
                excludes: list[str] = entry_cfg.get("excludes", []) if entry_cfg else []
                return _delete_extras(manifest_path, outpath, sub, excludes, opts=opts, entry=entry)
            return 0

        # Make the restore root's type agree before any transfer. In particular,
        # never let a directory sync walk through a symlink root, and never let
        # a single-file write follow one. s3transfer/direct downloads replace
        # inner leaves atomically; this handles the operation root itself.
        # A dry run reports the conflict instead of touching the root; the
        # dry-run sync then runs against the uncorrected root, so its transfer
        # report may differ from what the real (post-replacement) pull does.
        if has_data and not opts.meta_only and os.path.lexists(outpath):
            if is_dir:
                if os.path.islink(outpath) or not os.path.isdir(outpath):
                    if opts.dryrun:
                        write_output(f"(dry-run) would replace {outpath} (conflicting type)\n")
                    else:
                        os.remove(outpath)
                        os.makedirs(outpath, exist_ok=True)
            elif not stat_mod.S_ISREG(os.lstat(outpath).st_mode):
                if opts.dryrun:
                    write_output(f"(dry-run) would replace {outpath} (conflicting type)\n")
                elif os.path.islink(outpath) or not os.path.isdir(outpath):
                    os.remove(outpath)
                else:
                    shutil.rmtree(outpath)

        # 3. Normal path: prep, then sync (dir) or cp (file). Root correction
        # must precede the Windows writable pass so that pass cannot traverse a
        # symlinked restore root and chmod a file outside the destination.
        prep: list[tuple[str, int]] = []
        if IS_WINDOWS and not opts.meta_only and not opts.dryrun:
            prep = windows_collect_writable_prep(outpath, is_dir, manifest_path, sub)

        changed = False
        if not opts.meta_only and has_data:
            # The compare only matters for the dir sync; a single-file transfer
            # always happens (we only reach it on a manifest mismatch). Its
            # size (from the manifest) routes a large file through multipart.
            compare = sync_compare(cfg, opts, entry, manifest_path, sub=sub) if is_dir else None
            file_size = None if is_dir else _single_file_size(manifest_path)
            try:
                rc, changed = download_from_s3(
                    cfg,
                    entry,
                    outpath,
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

        # 4. Apply manifest metadata (mode, mtime, symlinks): objectless or
        #    metadata-only diffs (empty dirs, symlinks, mode/mtime) have nothing
        #    to download yet still need applying. apply_manifest sets the modes
        #    itself, so the writable prep needs no separate restore. Skipped
        #    with --data-only, and after a --checksum pass over an
        #    already-clean tree (nothing transferred, metadata matches).
        if opts.data_only:
            if IS_WINDOWS and not opts.meta_only:
                windows_restore_modes(prep)
            st = 0
        elif manifest_matches and not changed:
            if IS_WINDOWS and not opts.meta_only:
                windows_restore_modes(prep)
            st = 0
        elif opts.dryrun:
            # The same gate as the real apply: report that metadata (mode /
            # mtime / symlinks) would be applied, without mutating anything.
            write_output(f"(dry-run) would apply manifest metadata: {outpath}\n")
            st = 0
        else:
            st = apply_manifest(outpath, is_dir, manifest_path, sub=sub)

        if not opts.meta_only and opts.delete and is_dir:
            excludes = entry_cfg.get("excludes", []) if entry_cfg else []
            delete_status = _delete_extras(
                manifest_path, outpath, sub, excludes, opts=opts, entry=entry
            )
            if st == 0:
                st = delete_status

        return st
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


# The two sides of the status / pull --delete diff: the manifest and a fresh
# local walk, each as an ascending (entry_sort_key, item) stream that
# manifest.merge_join pairs up in one pass - constant memory, so a manifest
# far larger than RAM still works. rels on both sides are sub-relative and
# '.'-free ("." for the root itself, "x/y" below it).


def _manifest_keyed(
    manifest_path: str, sub: str | None
) -> Iterator[tuple[str, tuple[str, ManifestEntry]]]:
    """Stream ``(sort_key, (rel, record))`` for every record at/under ``sub``."""
    for entry in manifest.iter_manifest(manifest_path):
        rel = resolve_manifest_rel(entry.path, sub)
        if rel is None:
            continue
        yield manifest.entry_sort_key(rel, entry.is_dir), (rel, entry)


def _local_keyed(
    outpath: str, excludes: list[str]
) -> Iterator[tuple[str, tuple[str, os.stat_result, str | None]]]:
    """Stream ``(sort_key, (rel, lstat, sym_target))`` for the local tree.

    The manifest walk under ``outpath``, excludes applied outpath-relative -
    an excluded path is invisible to the diff on both lanes (never compared,
    never a local extra). A missing ``outpath`` yields nothing, so status
    degrades to reporting every record D."""
    if not os.path.lexists(outpath):
        return
    for rel, st, sym in localwalk.walk_tree(outpath, excludes):
        norm = "." if rel == "." else rel.removeprefix("./")
        yield manifest.entry_sort_key(rel, stat_mod.S_ISDIR(st.st_mode)), (norm, st, sym)


def _delete_extras(
    manifest_path: str,
    outpath: str,
    sub: str | None,
    excludes: list[str],
    *,
    opts: Opts,
    entry: str,
) -> int:
    """Remove local paths the manifest does not record (pull ``--delete``),
    behind the per-item confirmation (--yes answers every question yes; a
    non-interactive run without --yes answers no, i.e. removes nothing).

    The local-only lane of the merge-join; only the extras themselves are
    collected (never the whole key set) so the deepest-first removal order
    costs memory proportional to what is actually deleted."""
    confirmer: DeleteConfirmer | None = None
    if not opts.dryrun:
        mode = resolve_answer_mode(yes=opts.yes)
        if mode is AnswerMode.ALL_NO:
            return 0  # every answer is no: keep every extra, successfully
        if mode is AnswerMode.ASK:
            confirmer = DeleteConfirmer(mode, entry)
    extras: list[tuple[str, bool]] = []
    for _key, m, loc in manifest.merge_join(
        _manifest_keyed(manifest_path, sub), _local_keyed(outpath, excludes)
    ):
        if m is None and loc is not None:
            rel, st, _sym = loc
            if rel != ".":
                extras.append((os.path.join(outpath, rel), stat_mod.S_ISDIR(st.st_mode)))
    try:
        return 1 if remove_extras(extras, dryrun=opts.dryrun, confirm=confirmer) else 0
    finally:
        if confirmer is not None:
            confirmer.close()


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
            _manifest_keyed(manifest_path, sub), _local_keyed(outpath, excludes)
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
        for name in cfg.store.list_top_level_names(verbose=opts.verbose):
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
