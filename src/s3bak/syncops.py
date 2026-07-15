# Requires Python 3.10+
"""The manifest <-> S3 bridge and download orchestration.

Writes v3 manifests to S3 (full and sub-tree patch), downloads a manifest or
a data tree, and builds the sync update-lane strategy (``S3.sync``'s
``update_filter``). This is the seam between the pure manifest format
(manifest.py), the S3 backend (store.py), and the command layer (commands.py).
"""

from __future__ import annotations

import itertools
import os
import stat as stat_mod
import tempfile
from collections.abc import Iterator
from typing import Any

from s3bak import localwalk, manifest
from s3bak.config import Config, Opts
from s3bak.console import note_warning, write_output, write_stderr
from s3bak.manifest import ManifestEntry


def write_manifest_to_aws(
    cfg: Config,
    entry: str,
    target: str,
    excludes: list[str],
    verbose: bool,
    *,
    old_manifest: str | None = None,
    keep_old: manifest.KeepOld = False,
) -> None:
    """Walk `target` in S3 key order, stream the v3 manifest to a temp file,
    and upload it. For a directory entry the walk is merged with
    `old_manifest` under the `keep_old` policy, so records of kept-but-
    locally-vanished files survive the rewrite (see manifest.write_merged).
    A single-file entry has one record and no merge."""
    key = manifest.manifest_key(entry)
    write_stderr(f"Updating {cfg.prefix}/{key}\n")

    fd, tmp = tempfile.mkstemp(suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if os.path.isdir(target):
                manifest.write_merged(
                    f,
                    old_manifest,
                    None,
                    localwalk.walk_tree(target, excludes),
                    keep_old=keep_old,
                    warn=note_warning,
                )
            else:
                st = os.lstat(target)
                sym = os.readlink(target) if stat_mod.S_ISLNK(st.st_mode) else None
                manifest.write_manifest(f, [(os.path.basename(target), st, sym)])
        assert cfg.store is not None
        cfg.store.put_file(key, tmp, verbose=verbose)
    finally:
        os.unlink(tmp)


def patch_manifest_subtree(
    cfg: Config,
    entry: str,
    target_root: str,
    sub: str,
    excludes: list[str],
    opts: Opts,
    *,
    keep_old: manifest.KeepOld = False,
) -> bool:
    """Download the manifest, replace the records under `sub`, and re-upload.

    target_root/sub may be a file, a symlink, or a directory. If it does not
    exist locally, the records under `sub` are simply removed. Old and new
    records are both in sort-key order, so this is a streaming merge
    (manifest.write_merged), not a read-all + sort.
    """
    key = manifest.manifest_key(entry)
    if opts.dryrun:
        print(f"(dry-run) would patch manifest: {key} (sub={sub})")
        return True

    fd_old, old_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd_old)
    fd_new, new_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd_new)  # reopened by name below; closing now avoids an fd leak on error
    try:
        have_old = download_manifest(cfg, entry, old_path, opts.verbose)
        if not have_old and not os.path.lexists(target_root):
            # Deleting a never-backed sub-path beneath a root that is gone has
            # no manifest state to update (and no root metadata from which to
            # create a valid directory manifest).
            return False
        local_sub = os.path.join(target_root, sub)
        new_entries: Iterator[tuple[str, os.stat_result, str | None]] = iter(())
        if os.path.lexists(local_sub):
            new_entries = localwalk.iter_subtree(local_sub, sub, excludes)
        if not have_old:
            # First-ever manifest for this entry, born from a sub-path push:
            # record the entry root too, so the manifest keeps the dir-entry
            # shape ('.'-rooted) and the root's metadata restores on pull.
            root_record = (".", os.lstat(target_root), None)
            new_entries = itertools.chain([root_record], new_entries)
        with open(new_path, "w", encoding="utf-8") as out:
            manifest.write_merged(
                out,
                old_path if have_old else None,
                sub,
                new_entries,
                keep_old=keep_old,
                warn=note_warning,
            )
        write_stderr(f"Updating {cfg.prefix}/{key}\n")
        assert cfg.store is not None
        cfg.store.put_file(key, new_path, verbose=opts.verbose)
        return True
    finally:
        os.unlink(old_path)
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
) -> Any:
    """Build the sync update-lane strategy (`S3.sync`'s `update_filter`): the
    stat-only streaming ManifestFilter by default, EtagComparison under
    --checksum. `manifest_path=None` (nothing on S3 yet) yields an empty filter,
    so every both-sides pair transfers - which, with the default create lane
    copying every new entry, is also the entire v2->v3 migration story: the
    first push re-uploads everything and writes the v3 manifest. The
    size+mtime-check window is resolved for `entry`.

    The ManifestFilter streams the manifest file, so the caller must `close()`
    it before unlinking the temp manifest (see cmd_push / cmd_pull)."""
    assert cfg.store is not None
    if opts.checksum:
        return cfg.store.content_compare()
    records: Iterator[tuple[str, ManifestEntry]] = iter(())
    if manifest_path is not None:
        records = manifest.iter_compare_records(manifest_path, sub=sub)
    return manifest.ManifestFilter(records, window_ns=cfg.window_ns_for(entry))


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
    compare: Any = None,
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
    # mismatch), so a successful download counts as changed -> apply_manifest
    # runs and restores mode/mtime. Matters on Windows, where apply_manifest is
    # skipped when nothing changed. `size` (from the manifest record) routes a
    # large file through multipart download; a small one is a direct GetObject.
    if dryrun:
        write_output(f"(dry-run) download: {cfg.prefix}/{rel} -> {outpath}\n")
        return 0, True
    if not cfg.store.get_object(rel, outpath, size=size, verbose=verbose):
        return 1, False
    return 0, True
