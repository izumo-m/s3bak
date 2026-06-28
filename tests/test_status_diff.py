"""status / diff / show behaviour against the live endpoint."""

from __future__ import annotations


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


def test_diff_shows_content_changes(ws):
    ws.write("data/a.txt", "one\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "a.txt").write_text("two\n")
    res = ws.run("diff", "data")
    assert "-one" in res.out
    assert "+two" in res.out


def test_show_streams_file_to_stdout(ws):
    ws.write("data/a.txt", "hello\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("show", str(ws.root / "data" / "a.txt"), expect_rc=0)
    assert res.out == "hello\n"
