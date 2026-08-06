# Requires Python 3.10+
"""The manifest walk, on boto3-s3's directory engine.

``walk_tree`` yields the ``(rel, lstat, sym_target)`` items that
``manifest.write_manifest`` records, in S3 key byte order (``foo.txt``
before ``foo/bar``). The traversal itself is boto3-s3's
``LocalFileGenerator`` - the same engine (and therefore the same sort
definition) the data sync walks with - configured for complete, no-follow
entry enumeration. ``ManifestWalker`` only customizes the exclusion
(docs/excludes.md):

- **complete enumeration**: boto3-s3 returns directories, broken symlinks,
  special files, and unreadable entries before filtering. With symlink following
  disabled, links surface as lstat-based leaves and are never descended. An
  unreadable directory degrades to its own record with no children (reported
  through ``walk_tree``'s ``warn`` when the caller wires one);
- **excludes as a per-entry filter**: every emitted entry is judged alone
  against the entry's ``Excludes`` (aws-cli semantics - the walked pages are
  filtered, so dropping a directory's own record does not hide its
  non-excluded children). Skipping a descent outright happens only for the
  provable ``dir/*`` shape, as an optimization that cannot change what the
  filter decides;
- **directories in-stream**: each directory's record is yielded between the
  sibling files that sort before it and its own children, which is what keeps
  the whole stream in manifest order.

Because this order is the sync's own compare-key order, a manifest can be
merge-joined against either a fresh walk (``manifest.merge_join``, the status /
pull ``--delete`` diff) or an S3 listing (``ManifestFilter``) in one pass.
"""

from __future__ import annotations

import os
import stat as stat_mod
from typing import TYPE_CHECKING

from boto3_s3 import LocalFileGenerator, LocalStorage, WalkChild
from boto3_s3.types import FileKind

from s3bak.excludes import Excludes

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from boto3_s3 import LocalFileInfo
    from boto3_s3.types import LocalScanOptions

# The one boto3-s3 walk warning that does NOT hide tree content: the
# invalid-timestamp epoch fallback keeps its entry (the record is built from the
# raw ``st_mtime_ns`` the fallback never touches), so it must not mark the scan
# incomplete. Every other warning names a skipped/vanished path. Matched in
# full, not as a substring: a path literally containing this text (e.g.
# ``.../invalid timestamp/secret``) would otherwise let a genuine gap warning
# slip past the completeness gate and let ``push --delete`` delete a live
# backup. Any unrecognized wording is treated as a gap (fail closed).
_INVALID_TIMESTAMP_WARNING = "File has an invalid timestamp. Passing epoch time as timestamp."


class ManifestWalker(LocalFileGenerator):
    """Apply an entry's excludes to the walk, per emitted entry.

    One instance serves one walk: it carries the compiled ``Excludes`` and
    the ``key_prefix`` that re-anchors a sub walk's compare keys at the
    entry root (``""`` for an entry walk, ``"{sub}/"`` for a sub-path walk).
    The filter runs over the emitted pages (``list_file_pages``), so every
    entry is judged alone - an excluded directory loses its own record but
    not its visible children. ``finalize_children`` skips a descent only for
    the provable ``dir/*`` shape (``Excludes.prunes_subtree``), where
    pruning cannot change what the filter decides.

    The walker also tracks scan completeness: a warning that hides real tree
    content (an unopenable directory, a path that vanished mid-walk) sets
    ``scan_incomplete``, which ``push --delete`` consults to refuse deletions
    decided on a partial local view (an S3 object whose local file the walk
    could not see is not an orphan). Every walk here runs the complete view
    (``enumerate_all_entries``), which enumerates special files and
    content-unreadable entries instead of warn-skipping them - so a warning
    IS a gap, with one exception: the invalid-timestamp fallback warns but
    keeps its entry (the record is built from the raw ``st_mtime_ns``, which
    the fallback does not touch). ``PushJournal`` marks the same flag for
    the gaps only it can see (an unreadable file it decided to transfer, a
    symlink racing away before its readlink).
    """

    def __init__(self, excludes: Excludes, key_prefix: str) -> None:
        self._excludes = excludes
        self._key_prefix = key_prefix
        self.scan_incomplete = False

    def _completeness_watch(self, notify: Callable[[str], None]) -> Callable[[str], None]:
        """Wrap a walk ``notify`` so every warning for missing content marks
        the scan incomplete before the message goes wherever it was going."""

        def wrapped(body: str) -> None:
            if body != _INVALID_TIMESTAMP_WARNING:
                self.scan_incomplete = True
            notify(body)

        return wrapped

    def triggers_warning(self, path: str, notify: Callable[[str], None]) -> bool:
        return super().triggers_warning(path, self._completeness_watch(notify))

    def classify_child(
        self,
        entry: os.DirEntry[str],
        full: str,
        dir_fd: int | None,
        *,
        options: LocalScanOptions,
        notify: Callable[[str], None],
    ) -> WalkChild | None:
        return super().classify_child(
            entry, full, dir_fd, options=options, notify=self._completeness_watch(notify)
        )

    def finalize_children(self, children: list[WalkChild]) -> list[WalkChild]:
        # The provable prune (docs/excludes.md): a relative ``dir/*``
        # pattern covers the directory key and every key beneath it, so the
        # walk may skip the whole descent. No other child is dropped here -
        # dropping a directory in this hook would also hide its children,
        # which the per-entry semantics forbid; they are judged one by one
        # in list_file_pages instead.
        kept: list[WalkChild] = []
        for child in children:
            assert child.info.compare_key is not None  # stamped by scan_children
            if child.info.kind == FileKind.DIRECTORY and self._excludes.prunes_subtree(
                self._key_prefix + child.info.compare_key
            ):
                continue
            kept.append(child)
        return self.normalize_sort(kept)

    def _keep(self, info: LocalFileInfo) -> bool:
        assert info.compare_key is not None  # stamped by scan_children
        # Directories arrive with a trailing "/" on their compare_key - the
        # key space the patterns are defined over. The walked root's
        # compare_key is "", so its entry-rooted key IS the prefix: "" for
        # an entry walk (the entry root is never matched) and "{sub}/" for a
        # sub walk, where the named sub is an ordinary judged path.
        # ``info.key`` is the absolute local path, the anchor absolute
        # patterns match against.
        return not self._excludes.excluded(self._key_prefix + info.compare_key, info.key)

    def list_file_pages(
        self, root: str, options: LocalScanOptions
    ) -> Iterator[list[LocalFileInfo]]:
        # The per-entry exclusion, applied to everything the walk emits -
        # the sync's pair stream and walk_tree alike consume these pages.
        # Dropping an entry here never affects the traversal itself: an
        # excluded directory's record disappears while its non-excluded
        # children still emit (only ``finalize_children``'s provable prune
        # skips a descent).
        for page in super().list_file_pages(root, options):
            kept = [info for info in page if self._keep(info)]
            if kept:
                yield kept


def sync_walker(excludes: list[str], sub: str | None = None) -> ManifestWalker:
    """The push sync's local-side walk: the manifest walk's exclusion over
    the complete view (``store.sync_up`` enumerates every entry into the
    pair stream for the journal; see docs/journal.md).

    Sharing ``ManifestWalker`` is what makes the sync and the manifest agree
    on what an exclude means - one predicate, one anchor. The filter applies
    to the LOCAL side only (the S3 listing is never filtered), so an object
    under an excluded path - uploaded before the exclude was added - surfaces
    to the sync as an ordinary orphan and ``push --delete`` can retire it,
    instead of the exclude hiding it from every lane forever. ``sub``
    re-anchors a sub walk's keys at ``{sub}/`` so the entry's patterns keep
    their entry-rooted meaning. Always a ``ManifestWalker`` - with no
    excludes the filter is a no-op - because the push delete lane reads the
    walker's ``scan_incomplete`` flag."""
    return ManifestWalker(Excludes(excludes), f"{sub}/" if sub else "")


def walk_tree(
    root: str,
    excludes: list[str],
    *,
    root_rel: str = ".",
    rel_prefix: str = "./",
    warn: Callable[[str], None] | None = None,
) -> Iterator[tuple[str, os.stat_result, str | None]]:
    """Walk a directory tree yielding ``(rel, lstat, sym_target | None)`` in
    S3 key order - the items one manifest record each.

    rel uses the manifest convention: ``root_rel`` for the root, ``rel_prefix``
    + path below it - for an entry walk that is "." / "./sub/file"; a sub-path
    walk passes "./{sub}" / "./{sub}/" so every rel (and thus every exclude
    match) stays anchored at the ENTRY root, where the configured patterns are
    defined. The entry root itself is never matched; a SUB walk's own root is
    an ordinary judged path and is omitted when excluded (its non-excluded
    children still walk). A missing ``root`` raises OSError (the caller
    checked existence);
    an unreadable directory keeps its record and loses its children, and a
    path that changes underfoot mid-walk is skipped - ``warn`` (when given)
    receives one message per such gap, so a caller can surface that it did not
    see the whole tree. ``status`` and ``diff`` pass ``warn`` too (an unreadable
    directory hides local-only files, so a silent walk would report a clean tree
    that is not). ``warn=None`` (pull's apply/--delete lanes) walks silently: a
    gap there is judged by the direct-lstat fallback or safely left un-deleted.
    """
    ex = Excludes(excludes)
    key_prefix = "" if rel_prefix == "./" else rel_prefix.removeprefix("./")
    st = os.lstat(root)
    # A SUB walk's own root is an ordinary judged path. Judge it exactly as
    # the walker would judge the same path as a child: a directory key keeps
    # the trailing "/" (key_prefix carries it) and its absolute anchor gets
    # the trailing separator boto3-s3 stamps on directory keys; a non-dir
    # root (the manifest said directory, the local path is now a file or
    # symlink) is judged by the slash-less forms.
    is_dir_root = stat_mod.S_ISDIR(st.st_mode)
    root_key = key_prefix if is_dir_root else key_prefix.rstrip("/")
    root_anchor = os.path.abspath(root).replace(os.sep, "/") + ("/" if is_dir_root else "")
    if not key_prefix or not ex.excluded(root_key, root_anchor):
        yield root_rel, st, None

    storage = LocalStorage(
        root,
        walker=ManifestWalker(ex, key_prefix),
        follow_symlinks=False,
        enumerate_all_entries=True,
    )
    for info in storage.walk_local(on_warning=warn):
        assert info.compare_key is not None and info.stat_result is not None
        if not info.compare_key:
            continue  # root was emitted above to preserve missing-root errors
        if info.kind == FileKind.DIRECTORY:
            # A directory's compare_key carries the sort-order trailing "/".
            yield rel_prefix + info.compare_key[:-1], info.stat_result, None
            continue
        sym: str | None = None
        if info.is_symlink:
            try:
                sym = os.readlink(info.key.replace("/", os.sep))
            except OSError:
                # Raced away (or changed type) between the scan and here; the
                # record is dropped, so the walk is incomplete.
                if warn is not None:
                    warn(f"Skipping file {info.key}. File changed during the walk.")
                continue
        yield rel_prefix + info.compare_key, info.stat_result, sym
