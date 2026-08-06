"""The manifest walk on boto3-s3's engine: order, records, excludes, re-rooting."""

from __future__ import annotations

import os

import pytest

from s3bak import localwalk

# --- order ----------------------------------------------------------------------


def test_walk_tree_is_in_s3_key_order(tmp_path):
    # "foo.txt" must sort BEFORE the "foo/" subtree ('.' < '/'), and a dir's
    # record must stand immediately before its children.
    (tmp_path / "foo").mkdir()
    (tmp_path / "foo" / "bar").write_text("b")
    (tmp_path / "foo.txt").write_text("f")
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / ".hidden").write_text("h")

    rels = [rel for rel, _st, _sym in localwalk.walk_tree(str(tmp_path), [])]
    assert rels == [".", "./.hidden", "./a.txt", "./foo.txt", "./foo", "./foo/bar"]


# --- record kinds ---------------------------------------------------------------


def test_walk_tree_records_empty_directory(tmp_path):
    (tmp_path / "empty").mkdir()
    rels = [rel for rel, _st, _sym in localwalk.walk_tree(str(tmp_path), [])]
    assert rels == [".", "./empty"]


def test_walk_tree_records_symlinks_as_leaves(tmp_path):
    # Symlinks are never followed: an ok link records its target, a broken
    # link is still recorded, and a link to a directory is a leaf - its
    # subtree must not be walked (no duplicate records through the link).
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f").write_text("x")
    os.symlink("real/f", tmp_path / "link_ok")
    os.symlink("nowhere", tmp_path / "link_broken")
    os.symlink("real", tmp_path / "link_dir")

    items = {rel: sym for rel, _st, sym in localwalk.walk_tree(str(tmp_path), [])}
    assert items == {
        ".": None,
        "./link_broken": "nowhere",
        "./link_dir": "real",
        "./link_ok": "real/f",
        "./real": None,
        "./real/f": None,
    }


def test_walk_tree_records_special_files(tmp_path):
    # The manifest describes the tree, so a FIFO is recorded (the data sync's
    # own scan is what refuses to transfer it).
    if not hasattr(os, "mkfifo"):
        pytest.skip("no mkfifo on this platform")
    os.mkfifo(tmp_path / "fifo")
    rels = [rel for rel, _st, _sym in localwalk.walk_tree(str(tmp_path), [])]
    assert rels == [".", "./fifo"]


def test_walk_tree_skips_unreadable_directory(tmp_path):
    # An unreadable subdirectory is silently skipped: its own record is kept,
    # its children are not, and the walk does not raise.
    (tmp_path / "good.txt").write_text("g")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "x").write_text("x")
    os.chmod(locked, 0)
    try:
        rels = [rel for rel, _st, _sym in localwalk.walk_tree(str(tmp_path), [])]
    finally:
        os.chmod(locked, 0o755)
    assert rels == [".", "./good.txt", "./locked"]


def test_walk_tree_missing_root_raises(tmp_path):
    with pytest.raises(OSError):
        list(localwalk.walk_tree(str(tmp_path / "gone"), []))


# --- excludes -------------------------------------------------------------------


def test_walk_tree_applies_excludes(tmp_path):
    (tmp_path / "keep.txt").write_text("k")
    (tmp_path / "skip.log").write_text("s")
    (tmp_path / "pruned").mkdir()
    (tmp_path / "pruned" / "x").write_text("x")

    rels = [rel for rel, _st, _sym in localwalk.walk_tree(str(tmp_path), ["*.log", "pruned/*"])]
    assert rels == [".", "./keep.txt"]


def test_walk_tree_excludes_symlink_at_pruned_name(tmp_path):
    # A symlink occupying the name a prune pattern targets is excluded too.
    os.symlink("elsewhere", tmp_path / "pruned")
    rels = [rel for rel, _st, _sym in localwalk.walk_tree(str(tmp_path), ["pruned/*"])]
    assert rels == ["."]


# --- iter_subtree (sub-path re-rooting) ------------------------------------------


def test_walk_tree_warns_when_symlink_races_away(tmp_path, monkeypatch):
    # A symlink that changes underfoot between the scan and its readlink is
    # skipped; the wired warn hook must hear about the gap instead of the
    # record vanishing silently.
    (tmp_path / "real").mkdir()
    os.symlink("real", tmp_path / "lnk")

    def raise_oserror(path, *args, **kwargs):
        raise OSError("raced")

    monkeypatch.setattr("s3bak.localwalk.os.readlink", raise_oserror)
    warns: list[str] = []
    rels = [rel for rel, _st, _sym in localwalk.walk_tree(str(tmp_path), [], warn=warns.append)]
    assert "./lnk" not in rels
    assert any("changed during the walk" in w for w in warns)
