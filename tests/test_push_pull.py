"""push / pull / ls-remote round-trips against the live endpoint."""

from __future__ import annotations

import os

from s3bak import cli


def test_push_uploads_objects_and_manifest(ws):
    ws.write("data/a.txt", "alpha")
    ws.write("data/sub/b.txt", "beta")
    ws.config({"data": {"path": str(ws.root / "data")}})

    ws.run("push", "data", expect_rc=0)

    keys = ws.keys()
    assert "data/a.txt" in keys
    assert "data/sub/b.txt" in keys
    assert "data-ls-l.txt" in keys  # the metadata manifest


def test_ls_remote_lists_entry(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("ls-remote", expect_rc=0)
    assert "data" in res.out.split()


def test_pull_unpushed_entry_reports_not_found(ws):
    # download_manifest -> get_object hits NotFoundError for a never-pushed
    # entry, which must still map to a clean "not found" (not a crash).
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    res = ws.run("pull", "data", "-o", str(ws.root / "out"))
    assert res.rc != 0
    assert "not found" in (res.err + res.out).lower()


def test_ls_remote_missing_subpath_errors(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("ls-remote", str(ws.root / "data" / "nope"))
    assert res.rc != 0
    assert "not found" in (res.err + res.out).lower()


def test_pull_restores_content(ws):
    ws.write("data/a.txt", "alpha")
    ws.write("data/sub/b.txt", "beta")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "restore"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert (dest / "a.txt").read_text() == "alpha"
    assert (dest / "sub" / "b.txt").read_text() == "beta"


def test_single_file_download_is_reported_as_changed(ws):
    # download_from_s3 must report a single-file download as changed, so pull
    # runs apply_manifest (and restores mode/mtime) - on Windows it is skipped
    # when nothing changed, and a single-file download used to always look
    # unchanged.
    f = ws.write("solo.txt", "v1")
    ws.config({"solo.txt": {"path": str(f)}})
    ws.run("push", "solo.txt", expect_rc=0)

    cfg = cli.load_config()
    dest = ws.root / "out.txt"
    rc, changed = cli.download_from_s3(cfg, "solo.txt", str(dest), is_dir=False, verbose=False)
    assert rc == 0
    assert changed is True
    assert dest.read_text() == "v1"


def test_single_file_pull_restores_original_mtime(ws):
    f = ws.write("solo.txt", "data")
    old = 1_600_000_000
    os.utime(f, (old, old))
    ws.config({"solo.txt": {"path": str(f)}})
    ws.run("push", "solo.txt", expect_rc=0)

    dest = ws.root / "out.txt"
    ws.run("pull", "solo.txt", "-o", str(dest), expect_rc=0)
    assert int(dest.stat().st_mtime) == old


def test_push_after_delete_removes_remote_and_reports_it(ws):
    ws.write("data/a.txt", "a")
    ws.write("data/b.txt", "b")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    assert "data/b.txt" in ws.keys()

    # Remove a file locally; the next push deletes the remote object (sync
    # --delete). The delete must render as a proper line, not 'download: None'.
    (ws.root / "data" / "b.txt").unlink()
    res = ws.run("push", "data", expect_rc=0)

    assert "data/b.txt" not in ws.keys()
    assert "data/a.txt" in ws.keys()
    assert "None" not in res.out
    assert "delete" in res.out


def test_pull_delete_removes_local_extras(ws):
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "restore"
    dest.mkdir()
    (dest / "extra.txt").write_text("e")
    ws.run("pull", "data", "-o", str(dest), "--delete", expect_rc=0)

    assert (dest / "keep.txt").read_text() == "k"
    assert not (dest / "extra.txt").exists()
