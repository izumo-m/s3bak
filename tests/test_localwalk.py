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


def test_walk_tree_judges_a_sub_roots_own_key(tmp_path):
    # A SUB walk's own root is an ordinary judged path (the entry root never
    # is): dir/* covers it and everything beneath; "cache/" omits only the
    # root itself while the children still walk.
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "c.txt").write_text("c")

    covered = list(
        localwalk.walk_tree(
            str(tmp_path / "cache"), ["cache/*"], root_rel="./cache", rel_prefix="./cache/"
        )
    )
    assert covered == []

    rels = [
        rel
        for rel, _st, _sym in localwalk.walk_tree(
            str(tmp_path / "cache"), ["cache/"], root_rel="./cache", rel_prefix="./cache/"
        )
    ]
    assert rels == ["./cache/c.txt"]


def test_walk_tree_judges_a_file_shaped_sub_root_by_its_file_key(tmp_path):
    # The manifest may say directory while the local sub is now a file: the
    # root is judged by its actual kind - the slash-less key - so a bare
    # name matches it and a directory pattern does not.
    (tmp_path / "notes.txt").write_text("n")

    gone = list(
        localwalk.walk_tree(
            str(tmp_path / "notes.txt"),
            ["notes.txt"],
            root_rel="./notes.txt",
            rel_prefix="./notes.txt/",
        )
    )
    assert gone == []

    rels = [
        rel
        for rel, _st, _sym in localwalk.walk_tree(
            str(tmp_path / "notes.txt"),
            ["notes.txt/"],
            root_rel="./notes.txt",
            rel_prefix="./notes.txt/",
        )
    ]
    assert rels == ["./notes.txt"]


def test_walk_tree_anchored_pattern_judges_root_and_child_alike(tmp_path):
    # The same directory must be judged identically whether it is reached as
    # a child of a full walk or is itself the walked sub root - the anchored
    # form gets the same trailing separator either way.
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "c.txt").write_text("c")
    anchor = str(tmp_path / "cache").replace(os.sep, "/") + "/"

    full = [rel for rel, _st, _sym in localwalk.walk_tree(str(tmp_path), [anchor])]
    assert full == [".", "./cache/c.txt"]

    sub = [
        rel
        for rel, _st, _sym in localwalk.walk_tree(
            str(tmp_path / "cache"), [anchor], root_rel="./cache", rel_prefix="./cache/"
        )
    ]
    assert sub == ["./cache/c.txt"]


def test_prune_never_changes_what_the_filter_decides(tmp_path, monkeypatch):
    # The dir/* descent skip is an optimization only: with it disabled, the
    # walk must yield exactly the same rels.
    from s3bak.excludes import Excludes

    (tmp_path / "keep.txt").write_text("k")
    (tmp_path / "cache" / "sub").mkdir(parents=True)
    (tmp_path / "cache" / "c.txt").write_text("c")
    (tmp_path / "cache" / "sub" / "d.log").write_text("d")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "x.log").write_text("x")
    patterns = ["cache/*", "*.log", "logs/"]

    pruned = [rel for rel, _st, _sym in localwalk.walk_tree(str(tmp_path), patterns)]
    monkeypatch.setattr(Excludes, "prunes_subtree", lambda self, key: False)
    unpruned = [rel for rel, _st, _sym in localwalk.walk_tree(str(tmp_path), patterns)]
    assert pruned == unpruned == [".", "./keep.txt"]


def test_walk_tree_keeps_symlink_at_a_dir_patterns_name(tmp_path):
    # aws-cli semantics: every path is judged alone by its own key. A
    # symlink named "pruned" has the key "pruned" (no trailing slash), which
    # "pruned/*" does not match - so it stays, unlike the directory it is
    # named after.
    os.symlink("elsewhere", tmp_path / "pruned")
    rels = [rel for rel, _st, _sym in localwalk.walk_tree(str(tmp_path), ["pruned/*"])]
    assert rels == [".", "./pruned"]


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
