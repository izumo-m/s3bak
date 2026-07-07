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


def test_status_of_missing_directory_tree_reports_each_child(ws):
    # When the whole local tree is gone, status must classify the entry as a
    # directory from the manifest (not os.path.isdir of the missing path), so
    # each record maps to its own child path instead of folding onto one and
    # printing duplicate lines.
    import shutil

    ws.write("data/a.txt", "alpha")
    ws.write("data/sub/b.txt", "beta")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    shutil.rmtree(ws.root / "data")

    res = ws.run("status", "data", expect_rc=0)
    d_targets = [ln.split(None, 1)[1] for ln in res.out.splitlines() if ln.startswith("D")]
    assert any(t.endswith("a.txt") for t in d_targets)
    assert any(t.endswith(os.path.join("sub", "b.txt")) for t in d_targets)
    assert len(d_targets) == len(set(d_targets))  # no folded duplicates


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


def test_show_dotfile_subpath(ws):
    # Regression: file names were once cleaned with lstrip("./"), which strips
    # *characters* and mangled a dotfile (".bashrc" -> "bashrc") into a
    # nonexistent S3 key.
    ws.write("data/.bashrc", "export A=1\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("show", str(ws.root / "data" / ".bashrc"), expect_rc=0)
    assert res.out == "export A=1\n"


def test_diff_dotfile_subpath(ws):
    # Same lstrip regression on the diff path.
    ws.write("data/.bashrc", "v1\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / ".bashrc").write_text("v2\n")
    res = ws.run("diff", str(ws.root / "data" / ".bashrc"))
    assert res.rc == 1
    assert "-v1" in res.out
    assert "+v2" in res.out


def test_status_of_unpushed_entry_reports_error(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    res = ws.run("status", "data")
    assert res.rc == 1
    assert "not found on s3" in res.err.lower()


def test_diff_of_unpushed_single_file_reports_error(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.write("data/new.txt", "n")
    res = ws.run("diff", str(ws.root / "data" / "new.txt"))
    assert res.rc == 1
    assert "not found on s3" in res.err.lower()
