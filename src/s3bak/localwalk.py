# Requires Python 3.10+
"""The manifest walk, on boto3-s3's directory engine.

``walk_tree`` / ``iter_subtree`` yield the ``(rel, lstat, sym_target)`` items
``manifest.write_manifest`` records, in S3 key byte order (``foo.txt`` before
``foo/bar``). The traversal itself is boto3-s3's ``LocalFileGenerator`` - the
same engine (and therefore the same sort definition) the data sync walks with -
customized by ``ManifestWalker`` into a backup-style walk:

- **lstat-based**: symlinks surface as leaves (never followed, so a symlinked
  directory is not descended) with the link's own stat;
- **no vetting**: everything lstat-able is recorded - broken symlinks, special
  files, unreadable files - because the manifest describes the tree, and the
  data sync's own scan is what warns about what it cannot read. An unreadable
  directory degrades silently to its own record with no children;
- **excludes as pruning**: a ``dir/*`` pattern drops the directory child before
  the walk descends, so an excluded subtree costs nothing;
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
from boto3_s3.types import FileKind, LocalFileInfo, LocalScanOptions

from s3bak.manifest import path_match, split_excludes

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from boto3_s3.localstorage import LoopDetector


class ManifestWalker(LocalFileGenerator):
    """``LocalFileGenerator`` specialized for the manifest walk (see the module
    docstring for the behaviour it implements). One instance serves one walk:
    it carries the walk's exclude patterns and the ``rel_prefix`` that anchors
    them at the entry root (``"./"``, or ``"./{sub}/"`` for a sub-path push)."""

    def __init__(self, prune: list[str], skip: list[str], rel_prefix: str) -> None:
        self._prune = prune
        self._skip = skip
        self._rel_prefix = rel_prefix

    def entry_stat_result(self, entry: os.DirEntry[str]) -> os.stat_result | None:
        # The one stat per entry, unfollowed: a symlink classifies as a leaf
        # (its mode is not S_IFDIR) carrying its own lstat. lstat cannot fail
        # on a broken link, so None here means the entry raced away.
        try:
            return entry.stat(follow_symlinks=False)
        except OSError:
            return None

    def should_ignore_entry(
        self,
        entry: os.DirEntry[str],
        full: str,
        dir_fd: int | None,
        st: os.stat_result,
        *,
        notify: Callable[[str], None],
    ) -> bool:
        # No vetting: everything lstat-able is recorded (see module docstring).
        return False

    def finalize_children(self, children: list[WalkChild]) -> list[WalkChild]:
        # The exclude pruning: rels are entry-rooted via rel_prefix, matching
        # what split_excludes anchored. Dropping a DIRECTORY child here is what
        # keeps the walk out of the excluded subtree entirely.
        kept: list[WalkChild] = []
        for child in children:
            assert child.info.compare_key is not None  # stamped by scan_children
            rel = self._rel_prefix + child.info.compare_key
            if child.info.kind == FileKind.DIRECTORY:
                if any(path_match(rel[:-1], p) for p in self._prune):
                    continue
            else:
                if any(path_match(rel, p) for p in self._skip):
                    continue
                # A symlink named like a pruned directory is excluded too (it
                # occupies the name the pattern targets).
                if child.info.is_symlink and any(path_match(rel, p) for p in self._prune):
                    continue
            kept.append(child)
        return self.normalize_sort(kept)

    def walk_dir(
        self,
        dir_path: str,
        options: LocalScanOptions,
        *,
        strip: int,
        notify: Callable[[str], None],
        detector: LoopDetector | None,
    ) -> Iterator[list[LocalFileInfo]]:
        # The base walk descends directories without yielding them; the
        # manifest needs their records, so re-implement the loop to emit each
        # directory's info just before descending - after the sibling files
        # that sort before it, which keeps the stream in manifest order. The
        # loop detector is unused: an lstat walk never follows a symlink, so
        # it cannot re-enter an ancestor.
        run: list[LocalFileInfo] = []
        for sort_name, info, _loop_key in self.scan_children(
            dir_path, strip=strip, follow_symlinks=options.follow_symlinks, notify=notify
        ):
            if info.kind != FileKind.DIRECTORY:
                run.append(info)
                continue
            if run:
                yield run
                run = []
            yield [info]
            yield from self.walk_dir(
                os.path.join(dir_path, sort_name),
                options,
                strip=strip,
                notify=notify,
                detector=None,
            )
        if run:
            yield run


def walk_tree(
    root: str, excludes: list[str], *, root_rel: str = ".", rel_prefix: str = "./"
) -> Iterator[tuple[str, os.stat_result, str | None]]:
    """Walk a directory tree yielding ``(rel, lstat, sym_target | None)`` in
    S3 key order - the items one manifest record each.

    rel uses the manifest convention: ``root_rel`` for the root, ``rel_prefix``
    + path below it - for an entry push that is "." / "./sub/file"; a sub-path
    push passes "./{sub}" / "./{sub}/" so every rel (and thus every exclude
    match) stays anchored at the ENTRY root, where the configured patterns are
    defined. A missing ``root`` raises OSError (the caller checked existence);
    an unreadable directory keeps its record and silently loses its children.
    """
    prune, skip = split_excludes(excludes)
    yield root_rel, os.lstat(root), None

    storage = LocalStorage(root, walker=ManifestWalker(prune, skip, rel_prefix))
    for info in storage.walk_local():
        assert info.compare_key is not None and info.stat_result is not None
        if info.kind == FileKind.DIRECTORY:
            # A directory's compare_key carries the sort-order trailing "/".
            yield rel_prefix + info.compare_key[:-1], info.stat_result, None
            continue
        sym: str | None = None
        if info.is_symlink:
            try:
                sym = os.readlink(info.key.replace("/", os.sep))
            except OSError:
                continue  # raced away between the scan and here
        yield rel_prefix + info.compare_key, info.stat_result, sym


def iter_subtree(
    local_sub: str, sub: str, excludes: list[str]
) -> Iterator[tuple[str, os.stat_result, str | None]]:
    """Walk items for a sub-path push: ``local_sub`` as recorded under
    ``./{sub}``. Handles the file / symlink / directory cases. The walk rels
    are entry-rooted, so the entry's exclude patterns apply exactly as they
    would in a full push."""
    st = os.lstat(local_sub)
    if stat_mod.S_ISLNK(st.st_mode):
        yield f"./{sub}", st, os.readlink(local_sub)
        return
    if not os.path.isdir(local_sub):
        yield f"./{sub}", st, None
        return
    yield from walk_tree(local_sub, excludes, root_rel=f"./{sub}", rel_prefix=f"./{sub}/")
