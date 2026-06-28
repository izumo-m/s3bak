"""Entry kinds: single-file entries, excludes, and `list`."""

from __future__ import annotations

import os


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


def test_list_shows_configured_entries(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("list", expect_rc=0)
    assert "data" in res.out
