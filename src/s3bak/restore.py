# Requires Python 3.10+
"""Pull-side filesystem operations: manifest-to-target path resolution,
applying recorded metadata (mode / mtime / symlinks), the Windows
read-only prep, and pruning local extras.

Everything here mutates (or reads) the local filesystem from a downloaded
manifest; it does not touch S3.
"""

from __future__ import annotations

import os
import shutil
import stat as stat_mod
import unicodedata

from s3bak import manifest
from s3bak.console import err, write_output
from s3bak.manifest import ManifestEntry

# =============================================================================
# Manifest target resolution (restore paths)
# =============================================================================


def resolve_pull_destination(
    entry: str,
    configured_path: str | None,
    sub: str | None,
    output: str | None,
) -> str | None:
    """Resolve one pull selector to the filesystem path it will restore."""
    if output is not None:
        outpath = output
    elif configured_path is not None:
        outpath = os.path.join(configured_path, sub) if sub else configured_path
    else:
        return None

    if outpath.endswith("/"):
        tail = sub if sub else entry
        outpath = os.path.join(outpath, tail)
    return outpath


def canonical_restore_path(path: str) -> str:
    """Canonicalize a restore path without following its final component."""
    absolute = os.path.abspath(path)
    parent_real = os.path.realpath(os.path.dirname(absolute) or ".")
    resolved = os.path.normpath(os.path.join(parent_real, os.path.basename(absolute)))
    return os.path.normcase(resolved)


def canonical_restore_comparison_path(path: str) -> str:
    """Conservative identity used to reject possibly overlapping restores."""
    canonical = canonical_restore_path(path)
    return unicodedata.normalize("NFD", canonical).casefold()


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
    resolved = canonical_restore_path(target)
    try:
        return os.path.commonpath((root_real, resolved)) == root_real
    except ValueError:  # different Windows drives
        return False


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
        except OSError as e:
            err(f"utime failed: {target}: {e}")
            ok = False
    try:
        os.chmod(target, mode)
    except OSError as e:
        err(f"chmod failed: {target}: {e}")
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
    # Resolve the root's parent chain but not its final component. The final
    # component may itself be a hostile symlink that a directory/symlink record
    # is about to replace; following it would both bless the outside target and
    # make later children look spuriously outside the newly created root.
    root_real = canonical_restore_path(outpath)

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

        try:
            local_mode = os.lstat(target).st_mode
        except OSError:
            err(f"expected file missing (sync did not place it): {target}")
            errors += 1
            continue
        if stat_mod.S_IFMT(local_mode) != stat_mod.S_IFMT(m_entry.mode):
            # In particular, never chmod/utime through a local symlink where a
            # regular file is recorded: --meta-only must not mutate the link's
            # target outside the restore tree.
            err(f"expected {m_entry.path} to have its recorded file type: {target}")
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
# remove_extras
# =============================================================================


def remove_extras(extras: list[tuple[str, bool]]) -> int:
    """Remove local extras (pull ``--delete``): ``(path, is_dir)`` pairs the
    status/--delete merge-join found on the local side only. ``is_dir`` is the
    lstat kind, so a symlink - even one pointing at a directory - is unlinked,
    never rmdir'd. Deepest-first (reverse path order), so a directory's
    children go before the rmdir that needs them gone; a failure (e.g. a
    non-empty directory that lost a child to an exclude) is reported so a
    requested mirror restore cannot return success while extras remain.
    Returns the number of failed removals."""
    errors = 0
    extras.sort(key=lambda x: x[0], reverse=True)
    for path, is_dir_entry in extras:
        try:
            if is_dir_entry:
                os.rmdir(path)
            else:
                os.remove(path)
            write_output(f"delete: {path}\n")
        except OSError as e:
            err(f"delete failed: {path}: {e}")
            errors += 1
    return errors
