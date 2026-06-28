"""Entry kinds: single-file entries, excludes, and `list`."""

from __future__ import annotations


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


def test_list_shows_configured_entries(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("list", expect_rc=0)
    assert "data" in res.out
