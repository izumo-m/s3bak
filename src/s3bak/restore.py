# Requires Python 3.10+
"""Pull-side filesystem operations: manifest-to-target path resolution,
applying recorded metadata (mode / mtime / symlinks), the Windows
read-only prep, pruning local extras, and the keyed manifest / local-walk
streams (manifest_keyed / local_keyed) whose merge-join drives the gated
metadata apply and, via commands, the status and pull --delete diffs.

Everything here mutates (or reads) the local filesystem from a downloaded
manifest; it does not touch S3.
"""

from __future__ import annotations

import os
import shutil
import stat as stat_mod
import unicodedata
from collections.abc import Iterator

from s3bak import localwalk, manifest
from s3bak.compare import compare_to_stat
from s3bak.confirm import DeleteConfirmer
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


def prepare_dir_conflicts(outpath: str, manifest_path: str, sub: str | None) -> int:
    """Replace a local symlink sitting where the manifest records a directory,
    BEFORE the data sync runs: the sync opens ``dir/file`` paths through
    whatever is at ``dir``, so a pre-existing symlink there would route the
    downloads outside the restore tree. The metadata apply would repair the
    type anyway - this makes the repair happen before any bytes move. Other
    conflicting types stay untouched here: a write through a regular file
    fails loudly instead of escaping, and apply_manifest settles it after the
    download. Symlinks are removed as links, never followed. Returns the
    number of conflicts that could not be cleared (each reported)."""
    errors = 0
    root_real = canonical_restore_path(outpath)
    for entry in manifest.iter_manifest(manifest_path):
        if not entry.is_dir:
            continue
        rel = resolve_manifest_rel(entry.path, sub)
        if rel is None or rel == ".":
            continue  # the pull corrects the restore root itself
        target = os.path.join(outpath, rel)
        # Records arrive parents-first, so by the time a child is checked its
        # ancestors are real directories and one islink test per record is
        # enough (a link behind a fixed parent cannot survive to this point).
        if not os.path.islink(target):
            continue
        if not within_root(root_real, target):
            err(f"manifest path escapes restore root, skipped: {entry.path}")
            errors += 1
            continue
        try:
            os.remove(target)
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            err(f"cannot replace symlink with recorded directory: {target}: {e}")
            errors += 1
    return errors


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


# The two sides of the status / pull --delete / apply diff: the manifest and a
# fresh local walk, each as an ascending (entry_sort_key, item) stream that
# manifest.merge_join pairs up in one pass - constant memory, so a manifest
# far larger than RAM still works. rels on both sides are sub-relative and
# '.'-free ("." for the root itself, "x/y" below it).


def manifest_keyed(
    manifest_path: str, sub: str | None
) -> Iterator[tuple[str, tuple[str, ManifestEntry]]]:
    """Stream ``(sort_key, (rel, record))`` for every record at/under ``sub``."""
    for entry in manifest.iter_manifest(manifest_path):
        rel = resolve_manifest_rel(entry.path, sub)
        if rel is None:
            continue
        yield manifest.entry_sort_key(rel, entry.is_dir), (rel, entry)


def local_keyed(
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


def _lstat_readlink(target: str) -> tuple[os.stat_result | None, str | None]:
    """lstat + readlink shaped like one ``local_keyed`` walk item (None = missing)."""
    try:
        st = os.lstat(target)
    except OSError:
        return None, None
    local_sym: str | None = None
    if stat_mod.S_ISLNK(st.st_mode):
        try:
            local_sym = os.readlink(target)
        except OSError:
            local_sym = ""
    return st, local_sym


def _place_symlink(target: str, st: os.stat_result | None, sym_target: str) -> None:
    """Create the recorded symlink at ``target``, clearing what ``st`` says is
    there. A symlink is removed as a link (never recursing into its target); a
    real dir (e.g. left by an older follow-symlinks backup) is removed
    wholesale. Windows distinguishes file and directory symlinks, so the
    link's own target is probed (best effort - it may not exist yet) to pick
    the kind; POSIX ignores the flag."""
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if st is not None:
        if stat_mod.S_ISDIR(st.st_mode):
            shutil.rmtree(target)
        else:
            os.remove(target)
    resolved = sym_target if os.path.isabs(sym_target) else os.path.join(parent or ".", sym_target)
    os.symlink(sym_target, target, target_is_directory=os.path.isdir(resolved))
    write_output(f"{target} -> {sym_target}\n")


def _apply_record(
    m_entry: ManifestEntry,
    target: str,
    st: os.stat_result | None,
    local_sym: str | None,
    *,
    window_ns: int,
    is_dir_entry: bool,
    deferred_dirs: list[tuple[str, ManifestEntry]],
    deferred_symlinks: list[tuple[str, ManifestEntry]],
) -> int:
    """Repair one manifest record against its local lstat; a record whose
    local state already matches (the shared ``compare_to_stat`` predicate) is
    left untouched. Returns the error count (0 or 1). Directory records are
    only structurally fixed here; their mode/mtime is deferred - and
    re-checked - by ``apply_manifest`` after every child mutation. A symlink
    replacing a local directory is deferred too: removing a subtree the lazy
    walk may not have descended into yet would crash the very stream feeding
    this record."""
    if m_entry.sym_target is not None:
        if compare_to_stat(m_entry, st, local_sym, window_ns=window_ns).is_match:
            return 0
        if st is not None and stat_mod.S_ISDIR(st.st_mode):
            deferred_symlinks.append((target, m_entry))
        else:
            _place_symlink(target, st, m_entry.sym_target)
        return 0

    # Symlinks are handled above; the recorded type distinguishes a
    # directory (including an empty one, which has no S3 object) from a
    # regular file that was recorded but never uploaded.
    if is_dir_entry and m_entry.is_dir:
        # A file or symlink where a directory belongs is a stale conflicting
        # type; clear it so the recorded directory is what ends up there
        # (the symlink branch above clears conflicting types the same way).
        if st is not None and not stat_mod.S_ISDIR(st.st_mode):
            os.remove(target)
            st = None
        if st is None:
            os.makedirs(target, exist_ok=True)
        deferred_dirs.append((target, m_entry))
        return 0

    if compare_to_stat(m_entry, st, local_sym, window_ns=window_ns).is_match:
        return 0
    if st is None:
        err(f"expected file missing (sync did not place it): {target}")
        return 1
    if stat_mod.S_IFMT(st.st_mode) != stat_mod.S_IFMT(m_entry.mode):
        # In particular, never chmod/utime through a local symlink where a
        # regular file is recorded: --meta-only must not mutate the link's
        # target outside the restore tree.
        err(f"expected {m_entry.path} to have its recorded file type: {target}")
        return 1
    write_output(f"{m_entry.perm_str} {target}\n")
    return 0 if _apply_meta(target, m_entry.perm_bits, m_entry.mtime_ns) else 1


def apply_manifest(
    outpath: str,
    is_dir: bool,
    manifest_path: str,
    sub: str | None = None,
    *,
    window_ns: int,
    excludes: list[str] | None = None,
) -> int:
    """Repair local state to match the manifest, touching (and reporting)
    only records whose local state differs - the shared size+mtime predicate
    plus mode, symlink target, and directory mtime. An mtime drift inside
    ``window_ns`` is a match and stays as it is.

    A directory entry consumes one merge-join of the manifest against a fresh
    local walk. The walk prunes ``excludes`` (an excluded subtree is never
    scanned) but serves purely as a stat cache: a record the walk did not
    pair up is judged from a direct lstat before anything is concluded, so a
    record under an excluded path - pull's data sync is exclude-blind - is
    still repaired, and a genuinely missing file still errors."""
    deferred_dirs: list[tuple[str, ManifestEntry]] = []
    deferred_symlinks: list[tuple[str, ManifestEntry]] = []
    errors = 0
    # A manifest is downloaded from S3 and may be corrupt or hostile. Only a
    # directory entry joins record-controlled paths onto outpath, so only it can
    # escape (a single-file entry always writes at outpath). Reject any record
    # that would create/chmod/symlink outside the restore root - via ".." , an
    # absolute path, or a write through a symlink an earlier record planted.
    # A record the walk paired up cannot escape (walk keys are real paths under
    # the root, walked without following symlinks), so only the fallback lane
    # is checked. Resolve the root's parent chain but not its final component.
    # The final component may itself be a hostile symlink that a
    # directory/symlink record is about to replace; following it would both
    # bless the outside target and make later children look spuriously outside
    # the newly created root.
    root_real = canonical_restore_path(outpath)

    if is_dir:
        for _key, m, loc in manifest.merge_join(
            manifest_keyed(manifest_path, sub), local_keyed(outpath, excludes or [])
        ):
            if m is None:
                continue  # local-only: pull --delete's lane, not apply's
            rel, m_entry = m
            target = outpath if rel == "." else os.path.join(outpath, rel)
            if loc is not None:
                _lrel, st, local_sym = loc
            else:
                # The walk prunes excludes, so "not walked" may mean hidden
                # rather than missing: judge from a direct lstat.
                if rel != "." and not within_root(root_real, target):
                    err(f"manifest path escapes restore root, skipped: {m_entry.path}")
                    errors += 1
                    continue
                st, local_sym = _lstat_readlink(target)
            errors += _apply_record(
                m_entry,
                target,
                st,
                local_sym,
                window_ns=window_ns,
                is_dir_entry=True,
                deferred_dirs=deferred_dirs,
                deferred_symlinks=deferred_symlinks,
            )
    else:
        for m_entry in manifest.iter_manifest(manifest_path):
            res = manifest_target(m_entry, outpath, is_dir, sub)
            if res is None:
                continue
            target, _rel = res
            st, local_sym = _lstat_readlink(target)
            errors += _apply_record(
                m_entry,
                target,
                st,
                local_sym,
                window_ns=window_ns,
                is_dir_entry=False,
                deferred_dirs=deferred_dirs,
                deferred_symlinks=deferred_symlinks,
            )

    # Symlink-over-directory replacements ran into nothing above (the lazy
    # walk may still have needed the subtree); the stream is exhausted now, so
    # replace for real. The lstat is retaken - the subtree may have gained
    # downloads meanwhile, and rmtree must see what is really there.
    for target, m_entry in deferred_symlinks:
        st, _sym = _lstat_readlink(target)
        assert m_entry.sym_target is not None
        _place_symlink(target, st, m_entry.sym_target)

    # Directory mode/mtime goes deepest-first, after every child mutation (the
    # downloads ran before apply; symlink recreation and dir creation above
    # bump parent dir mtimes). The stream-time stat is stale by then, so each
    # directory is re-checked fresh - and skipped when it already matches.
    deferred_dirs.sort(key=lambda x: x[0], reverse=True)
    for target, m_entry in deferred_dirs:
        st, _sym = _lstat_readlink(target)
        if st is None or not stat_mod.S_ISDIR(st.st_mode):
            err(f"expected {m_entry.path} to be a directory: {target}")
            errors += 1
            continue
        if compare_to_stat(m_entry, st, None, window_ns=window_ns).is_match:
            continue
        write_output(f"{m_entry.perm_str} {target}\n")
        if not _apply_meta(target, m_entry.perm_bits, m_entry.mtime_ns):
            errors += 1

    return 1 if errors else 0


# =============================================================================
# remove_extras
# =============================================================================


def remove_extras(
    extras: list[tuple[str, bool]], *, dryrun: bool = False, confirm: DeleteConfirmer | None = None
) -> tuple[int, int]:
    """Remove local extras (pull ``--delete``): ``(path, is_dir)`` pairs the
    status/--delete merge-join found on the local side only. ``is_dir`` is the
    lstat kind, so a symlink - even one pointing at a directory - is unlinked,
    never rmdir'd. Deepest-first (reverse path order), so a directory's
    children go before the rmdir that needs them gone; a failure (e.g. a
    non-empty directory that lost a child to an exclude) is reported so a
    requested mirror restore cannot return success while extras remain.
    ``dryrun`` reports each candidate in the same order without removing it.
    ``confirm`` asks per extra, in the same deepest-first order; keeping an
    item silently keeps its ancestor directories too (their rmdir could only
    fail), and a kept item is a choice, not a failure. Returns
    ``(failed_removals, removals)`` - the second drives the caller's
    directory-metadata re-settle, since every removal bumps its parent
    directory's mtime."""
    errors = 0
    removed = 0
    extras.sort(key=lambda x: x[0], reverse=True)
    # A set rather than a "last kept path" cursor: on Windows the reverse path
    # order can interleave siblings between a directory and its descendants
    # (`\` sorts above many printable characters).
    kept_ancestors: set[str] = set()
    for path, is_dir_entry in extras:
        if dryrun:
            write_output(f"(dry-run) delete: {path}\n")
            continue
        if confirm is not None:
            if is_dir_entry and path in kept_ancestors:
                continue  # keeping the child forces keeping the directory
            if not confirm.confirm(path):
                parent = os.path.dirname(path)
                while parent and parent not in kept_ancestors:
                    kept_ancestors.add(parent)
                    parent = os.path.dirname(parent)
                continue
        try:
            if is_dir_entry:
                os.rmdir(path)
            else:
                os.remove(path)
            removed += 1
            write_output(f"delete: {path}\n")
        except OSError as e:
            err(f"delete failed: {path}: {e}")
            errors += 1
    return errors, removed
