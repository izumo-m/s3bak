"""status / diff / show behaviour against the live endpoint."""

from __future__ import annotations

import os


def test_status_clean_then_reports_changes(ws):
    ws.write("data/a.txt", "alpha")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    # Nothing changed -> no output.
    res = ws.run("status", "data", expect_rc=0)
    assert res.out.strip() == ""

    # Modify one file, add another -> M and A lines.
    (ws.root / "data" / "a.txt").write_text("changed!!")
    (ws.root / "data" / "c.txt").write_text("new")
    res = ws.run("status", "data", expect_rc=0)
    lines = res.out.splitlines()
    assert any(line.startswith("M") and "a.txt" in line for line in lines)
    assert any(line.startswith("A") and "c.txt" in line for line in lines)


def test_status_missing_subpath_errors(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("status", str(ws.root / "data" / "nope"))
    assert res.rc != 0
    assert "not found" in (res.err + res.out).lower()


def test_status_detects_changed_symlink_target(ws):
    ws.write("data/real.txt", "r")
    ws.write("data/other.txt", "o")
    os.symlink("real.txt", ws.root / "data" / "link")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "link").unlink()
    os.symlink("other.txt", ws.root / "data" / "link")  # retarget
    res = ws.run("status", "data", expect_rc=0)
    assert "link" in res.out


def test_status_verbose_humanizes_large_size_diff(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "a.txt").write_text("y" * 5000)  # grow by ~5 KB
    res = ws.run("status", "--verbose", "data")
    assert "a.txt" in res.out
    assert "KB" in res.out  # humanized size detail shown in verbose mode


def test_status_verbose_reports_mtime_change(ws):
    f = ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    os.utime(f, (1_600_000_000, 1_600_000_000))  # change mtime only
    res = ws.run("status", "--verbose", "data")
    assert "a.txt" in res.out


def test_diff_shows_content_changes(ws):
    ws.write("data/a.txt", "one\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "a.txt").write_text("two\n")
    res = ws.run("diff", "data")
    assert "-one" in res.out
    assert "+two" in res.out


def test_diff_identical_returns_zero_no_output(ws):
    ws.write("data/a.txt", "same\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("diff", "data", expect_rc=0)
    assert res.out.strip() == ""


def test_diff_reports_new_local_file(ws):
    ws.write("data/a.txt", "a\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "new.txt").write_text("brand new\n")
    res = ws.run("diff", "data")
    assert res.rc == 1
    assert "brand new" in res.out


def test_diff_single_file_entry_shows_change(ws):
    f = ws.write("solo.txt", "v1\n")
    ws.config({"solo.txt": {"path": str(f)}})
    ws.run("push", "solo.txt", expect_rc=0)

    f.write_text("v2\n")
    res = ws.run("diff", "solo.txt")
    assert res.rc == 1
    assert "-v1" in res.out
    assert "+v2" in res.out


def test_diff_reports_removed_local_file(ws):
    ws.write("data/a.txt", "a\n")
    ws.write("data/gone.txt", "will be deleted\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "gone.txt").unlink()
    res = ws.run("diff", "data")
    assert res.rc == 1
    assert "will be deleted" in res.out


def test_diff_subpath_file(ws):
    ws.write("data/sub/b.txt", "v1\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "sub" / "b.txt").write_text("v2\n")
    res = ws.run("diff", str(ws.root / "data" / "sub" / "b.txt"))
    assert res.rc == 1
    assert "-v1" in res.out
    assert "+v2" in res.out


def test_show_streams_file_to_stdout(ws):
    ws.write("data/a.txt", "hello\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("show", str(ws.root / "data" / "a.txt"), expect_rc=0)
    assert res.out == "hello\n"
