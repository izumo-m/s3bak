"""push / pull / ls-remote round-trips against the live endpoint."""

from __future__ import annotations


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


def test_pull_restores_content(ws):
    ws.write("data/a.txt", "alpha")
    ws.write("data/sub/b.txt", "beta")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "restore"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert (dest / "a.txt").read_text() == "alpha"
    assert (dest / "sub" / "b.txt").read_text() == "beta"


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
