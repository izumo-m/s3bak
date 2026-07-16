"""pull's gated metadata apply: only records whose local state differs from
the manifest are repaired (and reported); matching records stay untouched -
no mtime snap inside the window, no symlink recreation, no output noise."""

from __future__ import annotations

import os
import stat


def _mtime_ns(p) -> int:
    return os.lstat(p).st_mtime_ns


def _mode(p) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def test_pull_reports_only_repaired_records(ws):
    a = ws.write("data/a.txt", "alpha")
    ws.write("data/sub/b.txt", "beta")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    os.utime(a, (2_000_000_000, 2_000_000_000))  # drift a.txt past the window
    res = ws.run("pull", "data", expect_rc=0)

    assert "a.txt" in res.out  # re-downloaded and its metadata re-applied
    assert "b.txt" not in res.out  # matching record: untouched, unreported


def test_pull_does_not_snap_mtime_drift_inside_window(ws):
    a = ws.write("data/a.txt", "alpha")
    b = ws.write("data/b.txt", "beta")
    ws.config({"data": {"path": str(ws.root / "data")}}, mtime_window=2)
    ws.run("push", "data", expect_rc=0)

    drifted = _mtime_ns(a) + 1_000_000_000  # +1s: inside the 2s window
    os.utime(a, ns=(drifted, drifted))
    # A real difference (umask-proof), so pull gets past the no-op gate.
    os.chmod(b, 0o600 if _mode(b) != 0o600 else 0o640)

    res = ws.run("pull", "data", expect_rc=0)
    assert "b.txt" in res.out
    assert "a.txt" not in res.out
    # The window is a rounding tolerance: a match is left exactly as it is,
    # not "corrected" to the recorded nanoseconds.
    assert _mtime_ns(a) == drifted


def test_pull_does_not_recreate_matching_symlink(ws):
    a = ws.write("data/a.txt", "alpha")
    ws.write("data/sub/keep.txt", "x")
    os.symlink("keep.txt", ws.root / "data" / "sub" / "link")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    link_ino = os.lstat(ws.root / "data" / "sub" / "link").st_ino
    os.utime(a, (2_000_000_000, 2_000_000_000))  # make the pull do real work

    res = ws.run("pull", "data", expect_rc=0)
    assert "->" not in res.out  # no symlink line: nothing recreated
    assert os.lstat(ws.root / "data" / "sub" / "link").st_ino == link_ino


def test_pull_restores_parent_dir_mtime_bumped_by_download(ws):
    ws.write("data/sub/b.txt", "beta")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    sub = ws.root / "data" / "sub"
    recorded = _mtime_ns(sub)

    (sub / "b.txt").unlink()  # bumps sub's mtime; pull re-downloads b.txt
    ws.run("pull", "data", expect_rc=0)

    # The deferred, re-checked dir pass restored sub's recorded mtime even
    # though only the download itself dirtied it - so the next pull is a no-op.
    assert _mtime_ns(sub) == recorded
    assert ws.run("pull", "data", expect_rc=0).out.strip() == ""


def test_pull_repairs_mode_only_change_without_download(ws):
    a = ws.write("data/a.txt", "alpha")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    recorded_mode = _mode(a)

    os.chmod(a, 0o600 if recorded_mode != 0o600 else 0o640)
    res = ws.run("pull", "data", expect_rc=0)

    assert "download:" not in res.out  # size+mtime match: metadata-only repair
    assert _mode(a) == recorded_mode
    assert "a.txt" in res.out


def test_pull_applies_metadata_under_excluded_path(ws):
    # The apply walk prunes excludes, but a record it therefore never paired
    # up is judged from a direct lstat - so a record left from a push before
    # the exclude was added is still repaired, matching pull's exclude-blind
    # data sync.
    c = ws.write("data/cache/c.txt", "cached")
    ws.write("data/a.txt", "alpha")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)  # cache/ recorded: no excludes yet
    recorded_mode = _mode(c)

    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/*"]}})
    os.chmod(c, 0o600 if recorded_mode != 0o600 else 0o640)

    res = ws.run("pull", "data", expect_rc=0)
    assert "download:" not in res.out
    assert _mode(c) == recorded_mode


def test_pull_meta_only_repairs_only_mismatched_records(ws):
    a = ws.write("data/a.txt", "alpha")
    ws.write("data/b.txt", "beta")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    recorded_mode = _mode(a)

    os.chmod(a, 0o600 if recorded_mode != 0o600 else 0o640)
    res = ws.run("pull", "--meta-only", "data", expect_rc=0)

    assert _mode(a) == recorded_mode
    assert "a.txt" in res.out
    assert "b.txt" not in res.out


def test_pull_checksum_dryrun_clean_tree_prints_nothing(ws):
    # --checksum bypasses the early no-op gate, so this exercises the dry-run
    # stand-in condition itself: a settled tree plans no transfers and no
    # metadata apply, so nothing prints.
    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.run("pull", "data", expect_rc=0)  # settle metadata

    res = ws.run("pull", "--checksum", "--dry-run", "data", expect_rc=0)
    assert res.out.strip() == ""


def test_single_file_pull_repairs_mode(ws):
    f = ws.write("solo.txt", "content")
    ws.config({"solo": {"path": str(f)}})
    ws.run("push", "solo", expect_rc=0)
    recorded_mode = _mode(f)

    os.chmod(f, 0o600 if recorded_mode != 0o600 else 0o640)
    ws.run("pull", "solo", expect_rc=0)

    assert _mode(f) == recorded_mode
