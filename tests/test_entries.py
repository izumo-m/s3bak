"""Entry kinds: single-file entries, excludes, and `list`."""

from __future__ import annotations

import os

import pytest


def test_single_file_entry_roundtrip_with_metadata(ws, s3):
    f = ws.write("solo.txt", "content\n")
    ws.config({"solo.txt": {"path": str(f)}})

    ws.run("push", "solo.txt", expect_rc=0)

    assert "solo.txt" in ws.keys()
    head = s3.head_object(Bucket=ws.bucket, Key=f"{ws.prefix}/solo.txt")
    assert "local-mtime" in head["Metadata"]

    res = ws.run("status", "solo.txt", expect_rc=0)
    assert res.out.strip() == ""

    dest = ws.root / "out.txt"
    ws.run("pull", "solo.txt", "-o", str(dest), expect_rc=0)
    assert dest.read_text() == "content\n"


def test_excludes_skip_matching_files(ws):
    ws.write("data/keep.txt", "k")
    ws.write("data/skip.log", "s")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*.log"]}})

    ws.run("push", "data", expect_rc=0)

    keys = ws.keys()
    assert "data/keep.txt" in keys
    assert "data/skip.log" not in keys


def test_symlinks_are_recorded_in_manifest_not_uploaded_as_data(ws):
    ws.write("data/real.txt", "real")
    ws.write("data/sub/x.txt", "insub")
    os.symlink("real.txt", ws.root / "data" / "link.txt")
    os.symlink("sub", ws.root / "data" / "linkdir")
    ws.config({"data": {"path": str(ws.root / "data")}})

    ws.run("push", "data", expect_rc=0)

    # symlinks must not be followed into data objects
    keys = ws.keys()
    assert "data/real.txt" in keys
    assert "data/sub/x.txt" in keys
    assert "data/link.txt" not in keys
    assert not any(k.startswith("data/linkdir/") for k in keys)

    # pull recreates them as symlinks from the manifest
    dest = ws.root / "restore"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert os.path.islink(dest / "link.txt")
    assert os.readlink(dest / "link.txt") == "real.txt"
    assert os.path.islink(dest / "linkdir")
    assert os.readlink(dest / "linkdir") == "sub"
    assert (dest / "real.txt").read_text() == "real"


def test_symlink_restore_replaces_existing_dir(ws):
    # Simulate an older follow-symlinks backup: the symlink path already holds a
    # real directory locally; restore must replace it with the symlink cleanly.
    ws.write("data/real.txt", "real")
    os.symlink("real.txt", ws.root / "data" / "link.txt")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "restore"
    (dest / "link.txt").mkdir(parents=True)
    (dest / "link.txt" / "stale.txt").write_text("stale")
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert os.path.islink(dest / "link.txt")
    assert os.readlink(dest / "link.txt") == "real.txt"


def test_empty_directory_subpath_pull_restores_a_directory(ws):
    ws.write("data/file.txt", "x")
    (ws.root / "data" / "empty").mkdir()
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    # The empty dir is in the manifest but has no S3 object; pulling it as a
    # sub-path must restore a directory, not fail as a missing single file.
    dest = ws.root / "out"
    ws.run("pull", str(ws.root / "data" / "empty"), "-o", str(dest), expect_rc=0)
    assert dest.is_dir()


def test_post_hook_failure_propagates(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data"), "post_hook": "exit 3"}})
    res = ws.run("push", "data")
    assert res.rc == 3


def test_post_hook_runs_on_success(ws):
    marker = ws.root / "hook-ran"
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data"), "post_hook": f"touch {marker}"}})
    ws.run("push", "data", expect_rc=0)
    assert marker.exists()


def test_windows_pull_applies_manifest_without_downloads(ws, monkeypatch):
    # On Windows, apply_manifest must run even when nothing was downloaded (an
    # empty-dir sub-path here): the restore must not be gated on sync_changed.
    from s3bak import cli

    monkeypatch.setattr(cli, "IS_WINDOWS", True)
    ws.write("data/file.txt", "x")
    (ws.root / "data" / "empty").mkdir()
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "out"
    ws.run("pull", str(ws.root / "data" / "empty"), "-o", str(dest), expect_rc=0)
    assert dest.is_dir()


def test_symlink_entry_path_is_rejected(ws):
    (ws.root / "realdir").mkdir()
    os.symlink("realdir", ws.root / "linkentry")
    ws.config({"linkentry": {"path": str(ws.root / "linkentry")}})

    res = ws.run("push", "linkentry")
    assert res.rc != 0
    assert "symlink" in (res.err + res.out).lower()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo on this platform")
def test_special_file_entry_path_is_rejected(ws):
    fifo = ws.root / "fifo"
    os.mkfifo(fifo)
    ws.config({"fifo": {"path": str(fifo)}})

    res = ws.run("push", "fifo")
    assert res.rc != 0
    assert "regular file or directory" in (res.err + res.out).lower()


def test_inner_symlink_subpath_pull_restores_symlink(ws):
    # A symlink inside a dir entry, pulled as a sub-path, must come back as a
    # symlink (no data object exists for it).
    ws.write("data/real.txt", "r")
    os.symlink("real.txt", ws.root / "data" / "link.txt")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "out"
    ws.run("pull", str(ws.root / "data" / "link.txt"), "-o", str(dest), expect_rc=0)
    assert os.path.islink(dest)
    assert os.readlink(dest) == "real.txt"


def test_list_shows_configured_entries(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("list", expect_rc=0)
    assert "data" in res.out
