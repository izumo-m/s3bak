# Requires Python 3.10+
"""Pull-side filesystem operations: manifest-to-target path resolution,
applying recorded metadata (mode / mtime / symlinks), the Windows
read-only prep, local-tree enumeration, and pruning local extras.

Everything here mutates (or reads) the local filesystem from a downloaded
manifest; it does not touch S3.
"""

from __future__ import annotations

import os
import shutil
import stat as stat_mod
from collections.abc import Iterator

from s3bak import manifest
from s3bak.console import err, write_output
from s3bak.manifest import ManifestEntry, path_match, split_excludes

# =============================================================================
# Manifest target resolution (restore paths)
# =============================================================================


def resolve_manifest_rel(rel_field: str, sub: str | None) -> str | None:
    """Translate manifest rel ('.' / './x/y' / 'basename') into the
    sub-relative form ('.' for self, 'x/y' for descendants, None to skip).
    """
    rel = rel_field.removeprefix("./")
    if rel_field == ".":
        rel = "."
    if sub is None:
        return rel
    if rel == sub:
        return "."
    if rel.startswith(sub + "/"):
        return rel[len(sub) + 1 :]
    return None


def manifest_target(
    entry: ManifestEntry, outpath: str, is_dir: bool, sub: str | None
) -> tuple[str, str] | None:
    """Resolve the manifest entry to (target_path, sub_rel) or None to skip."""
    rel = resolve_manifest_rel(entry.path, sub)
    if rel is None:
        return None
    if is_dir:
        target = outpath if rel == "." else os.path.join(outpath, rel)
    else:
        target = outpath
    return target, rel


def within_root(root_real: str, target: str) -> bool:
    """True iff writing at `target` stays inside `root_real` (a realpath'd
    restore root). Resolves symlinks in the parent chain, so a write *through*
    a symlinked ancestor is caught, while the final component may still be
    absent (about to be created) or a symlink we intend to replace."""
    parent_real = os.path.realpath(os.path.dirname(target) or ".")
    resolved = os.path.normpath(os.path.join(parent_real, os.path.basename(target)))
    return resolved == root_real or resolved.startswith(root_real + os.sep)


# =============================================================================
# Tree iteration
# =============================================================================


def iter_local_tree(outpath: str, excludes: list[str]) -> Iterator[tuple[str, bool]]:
    """Walk local tree yielding (rel_without_dot_slash, is_dir)."""
    prune_patterns, skip_patterns = split_excludes(excludes)

    for dirpath, dirnames, filenames in os.walk(outpath, followlinks=False):
        rel_dir = os.path.relpath(dirpath, outpath)
        rel_prefix = "./" if rel_dir == "." else f"./{rel_dir}/"

        dirnames.sort()
        filenames.sort()

        to_remove: list[str] = []
        for d in dirnames:
            rel = f"{rel_prefix}{d}"
            if any(path_match(rel, p) for p in prune_patterns):
                to_remove.append(d)
                continue
            yield rel[2:], True  # strip "./"
        for d in to_remove:
            dirnames.remove(d)

        for f in filenames:
            rel = f"{rel_prefix}{f}"
            if any(path_match(rel, p) for p in skip_patterns):
                continue
            yield rel[2:], False


# =============================================================================
# Manifest application (restore metadata)
# =============================================================================


def windows_collect_writable_prep(
    outpath: str, is_dir: bool, manifest_path: str, sub: str | None
) -> list[tuple[str, int]]:
    # Windows only. Walk the manifest, find existing local files that are:
    #   - regular files (not dir / not symlink)
    #   - read-only (owner write bit clear)
    # Temporarily add owner-write so `boto3-s3 sync`/`cp` can overwrite them.
    # Every read-only file is prepped, not just size+mtime-check failures: the
    # sync's copy decision can be broader than the local size+mtime check (remote
    # size drift; any content difference under --checksum), and prep must
    # never under-approximate what the sync may overwrite. apply_manifest
    # re-applies the recorded modes afterwards (or windows_restore_modes on
    # the failure/--data-only paths). Returns [(path, original_mode), ...].
    targets: list[tuple[str, int]] = []
    try:
        for entry in manifest.iter_manifest(manifest_path):
            if entry.sym_target is not None or not entry.is_file:
                continue
            res = manifest_target(entry, outpath, is_dir, sub)
            if res is None:
                continue
            target, _rel = res
            try:
                st = os.lstat(target)
            except OSError:
                continue
            if not stat_mod.S_ISREG(st.st_mode):
                continue
            if st.st_mode & stat_mod.S_IWRITE:
                continue
            try:
                os.chmod(target, st.st_mode | stat_mod.S_IWRITE)
            except OSError:
                continue
            targets.append((target, st.st_mode))
    except OSError:
        pass
    return targets


def windows_restore_modes(targets: list[tuple[str, int]]) -> None:
    for target, original_mode in targets:
        try:
            os.chmod(target, original_mode)
        except OSError:
            continue


def _apply_meta(target: str, mode: int, mtime_ns: int | None) -> bool:
    ok = True
    if mtime_ns is not None:
        try:
            os.utime(target, ns=(mtime_ns, mtime_ns))
        except PermissionError as e:
            err(f"utime failed (not owner?): {target}: {e}")
            ok = False
    try:
        os.chmod(target, mode)
    except PermissionError as e:
        err(f"chmod failed (not owner?): {target}: {e}")
        ok = False
    return ok


def apply_manifest(outpath: str, is_dir: bool, manifest_path: str, sub: str | None = None) -> int:
    deferred_dirs: list[tuple[str, int, int | None]] = []
    errors = 0
    # A manifest is downloaded from S3 and may be corrupt or hostile. Only a
    # directory entry joins record-controlled paths onto outpath, so only it can
    # escape (a single-file entry always writes at outpath). Reject any record
    # that would create/chmod/symlink outside the restore root - via ".." , an
    # absolute path, or a write through a symlink an earlier record planted.
    root_real = os.path.realpath(outpath)

    for m_entry in manifest.iter_manifest(manifest_path):
        res = manifest_target(m_entry, outpath, is_dir, sub)
        if res is None:
            continue
        target, rel = res
        if is_dir and rel != "." and not within_root(root_real, target):
            err(f"manifest path escapes restore root, skipped: {m_entry.path}")
            errors += 1
            continue
        mode = m_entry.perm_bits

        if m_entry.sym_target is not None:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            # Clear whatever is already there. islink first, so a symlink is
            # removed as a link (never recursing into its target); a real dir
            # (e.g. left by an older follow-symlinks backup) is removed wholesale.
            if os.path.islink(target) or os.path.isfile(target):
                os.remove(target)
            elif os.path.isdir(target):
                shutil.rmtree(target)
            os.symlink(m_entry.sym_target, target)
            write_output(f"{target} -> {m_entry.sym_target}\n")
            continue

        # Symlinks are handled above; the recorded type distinguishes a
        # directory (including an empty one, which has no S3 object) from a
        # regular file that was recorded but never uploaded.
        if is_dir and m_entry.is_dir:
            # A file or symlink where a directory belongs is a stale conflicting
            # type; clear it so the recorded directory is what ends up there
            # (the symlink branch above clears conflicting types the same way).
            if os.path.islink(target) or (os.path.exists(target) and not os.path.isdir(target)):
                os.remove(target)
            if not os.path.isdir(target):
                os.makedirs(target, exist_ok=True)
            deferred_dirs.append((target, mode, m_entry.mtime_ns))
            continue

        if not os.path.exists(target):
            err(f"expected file missing (sync did not place it): {target}")
            errors += 1
            continue

        write_output(f"{m_entry.perm_str} {target}\n")
        if not _apply_meta(target, mode, m_entry.mtime_ns):
            errors += 1

    deferred_dirs.sort(key=lambda x: x[0], reverse=True)
    for target, mode, mtime_ns in deferred_dirs:
        write_output(f"{format(mode, 'o')} {target}\n")
        if not _apply_meta(target, mode, mtime_ns):
            errors += 1

    return 1 if errors else 0


# =============================================================================
# delete_extra_files
# =============================================================================


def delete_extra_files(
    outpath: str,
    check_only: bool,
    remote_files: dict[str, int],
    excludes: list[str],
) -> bool:
    extras: list[tuple[str, bool]] = []
    for rel, is_dir_entry in iter_local_tree(outpath, excludes):
        if not rel or rel == ".":
            continue
        if rel not in remote_files:
            extras.append((os.path.join(outpath, rel), is_dir_entry))

    if not extras:
        return False

    extras.sort(key=lambda x: x[0], reverse=True)

    for path, is_dir_entry in extras:
        if check_only:
            write_output(f"A {path}\n")
        else:
            try:
                if is_dir_entry and not os.path.islink(path):
                    os.rmdir(path)
                else:
                    # Files and symlinks (incl. symlinks to directories, which
                    # iter_local_tree reports as is_dir) are unlinked.
                    os.remove(path)
                write_output(f"delete: {path}\n")
            except OSError:
                pass

    return True
