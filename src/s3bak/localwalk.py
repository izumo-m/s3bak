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
  non-excluded children). Dropping an entry by its name before its stat,
  and with it a directory's whole descent, happens only where the patterns
  provably exclude the entry (and, for a directory, everything below it), as
  an optimization that cannot change what the filter decides;
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


class _ExcludingWalker(LocalFileGenerator):
    """What both excluding walks share: dropping an entry by its name.

    ``classify_child`` is the walk's one stat per entry. ``_name_excluded``
    decides ahead of it, from the entry's name and its scanned type alone,
    that the excludes rule the entry out whatever the stat would say: a file
    or symlink whose own key is excluded, or a directory under which every
    key is (``Excludes.excludes_below``). Such an entry is not stat'ed,
    vetted or warned about, and such a directory is not read - what the walk
    yields is unchanged, since the entry would have been dropped after its
    stat, and a directory's every descendant after theirs. With symlinks
    not followed a link is a leaf keyed without a trailing ``/``, so its
    name decides it too; a followed one waits for its stat, which says what
    it leads to.

    ``key_prefix`` re-anchors keys at the entry root (``""`` for an entry
    walk, ``"{sub}/"`` for a sub-path walk). ``anchor`` is the absolute path
    the walked root stands for when absolute patterns are judged, ``None``
    for the walked root itself.
    """

    def __init__(self, excludes: Excludes, key_prefix: str, anchor: str | None = None) -> None:
        self._excludes = excludes
        self._key_prefix = key_prefix
        self._anchor = anchor
        self._root = ""

    def list_file_pages(
        self, root: str, options: LocalScanOptions
    ) -> Iterator[list[LocalFileInfo]]:
        # ``root`` is absolute with a trailing separator, and every entry's
        # ``full`` path is joined beneath it.
        self._root = root
        yield from super().list_file_pages(root, options)

    def _keys(self, full: str, is_dir: bool) -> tuple[str, str]:
        """The entry-rooted key and the absolute anchor of the entry at
        ``full``, as the walk stamps them (a directory's with a trailing
        ``/``)."""
        rel = full[len(self._root) :]
        slash = "/" if is_dir else ""
        key = self._key_prefix + rel.replace(os.sep, "/") + slash
        path = full if self._anchor is None else os.path.join(self._anchor, rel)
        return key, path.replace(os.sep, "/") + slash

    def _name_excluded(self, entry: os.DirEntry[str], full: str, options: LocalScanOptions) -> bool:
        if self._excludes.empty:
            return False
        try:
            if entry.is_symlink():
                if options.follow_symlinks:
                    return False
                is_dir = False
            else:
                is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            return False  # left to the stat, which reports what is wrong
        key, anchor = self._keys(full, is_dir)
        if is_dir:
            return self._excludes.excludes_below(key, anchor)
        return self._excludes.excluded(key, anchor)


class ManifestWalker(_ExcludingWalker):
    """Apply an entry's excludes to the walk, per emitted entry.

    One instance serves one walk: it carries the compiled ``Excludes`` and
    the ``key_prefix`` that re-anchors a sub walk's compare keys at the
    entry root (``""`` for an entry walk, ``"{sub}/"`` for a sub-path walk).
    The filter runs over the emitted pages (``list_file_pages``), so every
    entry is judged alone - an excluded directory loses its own record but
    not its visible children. Ahead of the stat, ``classify_child`` drops
    what the excludes rule out by name alone (``_ExcludingWalker``), which
    is where a directory everything under which is excluded is skipped
    without being read; neither can change what the filter decides.

    The walker also tracks scan completeness: a warning that hides real tree
    content (an unopenable directory, a path that vanished mid-walk) sets
    ``scan_incomplete``, which ``push --delete`` consults to refuse deletions
    decided on a partial local view (an S3 object whose local file the walk
    could not see is not an orphan). Every walk here runs the complete view
    (``enumerate_all_entries``), which enumerates special files and
    content-unreadable entries instead of warn-skipping them - so a warning
    IS a gap, with one exception: the invalid-timestamp fallback warns but
    keeps its entry (the record is built from the raw ``st_mtime_ns``, which
    the fallback does not touch). An entry dropped by name never warns, so
    a gap under a skipped subtree - which nothing could record or delete -
    does not count. ``PushJournal`` marks the same flag for the gaps only it
    can see (an unreadable file it decided to transfer, a symlink racing
    away before its readlink).
    """

    def __init__(self, excludes: Excludes, key_prefix: str) -> None:
        super().__init__(excludes, key_prefix)
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
        if self._name_excluded(entry, full, options):
            return None
        return super().classify_child(
            entry, full, dir_fd, options=options, notify=self._completeness_watch(notify)
        )

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
        # children still emit. The by-name drop in classify_child has
        # already removed most excluded entries; this catches the rest - an
        # excluded directory that is still descended, and an entry whose
        # stat disagreed with its scanned type.
        for page in super().list_file_pages(root, options):
            kept = [info for info in page if self._keep(info)]
            if kept:
                yield kept


class PullDestWalker(_ExcludingWalker):
    """Pull's destination-side walk: the transfer view, without the excluded
    paths.

    Pull's exclusion lives in its lanes (``commands._pull_exclude_lanes``),
    which veto every excluded key whichever side it shows up on, so the
    destination walk need not show them one. It drops what the excludes rule
    out by name before the stat (``_ExcludingWalker``) - an excluded file,
    and a directory everything under which is excluded, which is then not
    read at all. The lanes decide exactly as before; what changes is that
    an excluded subtree costs nothing, and that the vetting, which
    warn-skips special files and unreadable entries while it scans, never
    sees a socket under ``~/.ssh/agent/*``. A warning about an entry that
    is excluded but still walked (a directory something below it is taken
    back from) is dropped as well. ``sub`` re-anchors keys at the entry
    root, and ``anchor`` is the restore destination the absolute patterns
    match against (a staged pull walks the stage, but the patterns are
    defined against where the tree ends up).
    """

    def __init__(self, excludes: Excludes, sub: str | None, anchor: str) -> None:
        super().__init__(excludes, f"{sub}/" if sub else "", os.path.abspath(anchor))

    def classify_child(
        self,
        entry: os.DirEntry[str],
        full: str,
        dir_fd: int | None,
        *,
        options: LocalScanOptions,
        notify: Callable[[str], None],
    ) -> WalkChild | None:
        if self._name_excluded(entry, full, options):
            return None
        return super().classify_child(
            entry, full, dir_fd, options=options, notify=self._unless_excluded(entry, full, notify)
        )

    def _unless_excluded(
        self, entry: os.DirEntry[str], full: str, notify: Callable[[str], None]
    ) -> Callable[[str], None]:
        """Wrap a walk ``notify`` so a warning reaches it only when the entry
        is not excluded."""

        def wrapped(body: str) -> None:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if not self._excludes.excluded(*self._keys(full, is_dir)):
                notify(body)

        return wrapped


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
