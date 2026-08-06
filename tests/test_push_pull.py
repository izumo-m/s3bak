"""push / pull / ls-remote round-trips against the live endpoint."""

from __future__ import annotations

import json
import os
import shutil

import pytest

from s3bak import cli
from s3bak.compare import SYMLINK_MTIME_SUPPORTED

_NO_SYMLINK_MTIME_REASON = "platform cannot set a symlink's own mtime without following it"
_DRIFTED_LINK_MTIME_NS = 1_700_000_000_000_000_000


def _manifest_body(ws, entry: str) -> str:
    key = f"{ws.prefix}/{entry}-manifest.jsonl"
    return ws.s3.get_object(Bucket=ws.bucket, Key=key)["Body"].read().decode()


def test_push_uploads_objects_and_manifest(ws):
    ws.write("data/a.txt", "alpha")
    ws.write("data/sub/b.txt", "beta")
    ws.config({"data": {"path": str(ws.root / "data")}})

    ws.run("push", "data", expect_rc=0)

    keys = ws.keys()
    assert "data/a.txt" in keys
    assert "data/sub/b.txt" in keys
    assert "data-manifest.jsonl" in keys  # the metadata manifest


def test_first_push_of_empty_directory_writes_restorable_manifest(ws):
    (ws.root / "empty").mkdir()
    ws.config({"empty": {"path": str(ws.root / "empty")}})

    ws.run("push", "empty", expect_rc=0)

    assert ws.keys() == {"empty-manifest.jsonl"}
    dest = ws.root / "restored-empty"
    ws.run("pull", "empty", "-o", str(dest), expect_rc=0)
    assert dest.is_dir()


def test_push_records_new_empty_directory_without_data_transfer(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    (ws.root / "data" / "empty").mkdir()

    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "restored"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert (dest / "empty").is_dir()


def test_push_records_changed_symlink_target_without_data_transfer(ws):
    ws.write("data/a.txt", "a")
    ws.write("data/b.txt", "b")
    os.symlink("a.txt", ws.root / "data" / "link")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    (ws.root / "data" / "link").unlink()
    os.symlink("b.txt", ws.root / "data" / "link")

    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "restored"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert os.readlink(dest / "link") == "b.txt"


def test_push_refreshes_manifest_on_file_mode_change(ws):
    p = ws.write("data/a.txt", "alpha")
    os.chmod(p, 0o644)
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    os.chmod(p, 0o600)

    res = ws.run("push", "data", expect_rc=0)

    assert "upload:" not in res.out  # a chmod re-transfers no data
    assert "Updating" in res.err
    assert '"mode":"100600"' in _manifest_body(ws, "data")
    assert ws.run("status", "data", expect_rc=0).out.strip() == ""
    # Settled: the next push rewrites nothing.
    res = ws.run("push", "data", expect_rc=0)
    assert "Updating" not in res.err


def test_push_refreshes_manifest_on_directory_mode_change(ws):
    ws.write("data/sub/b.txt", "beta")
    os.chmod(ws.root / "data" / "sub", 0o755)
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    os.chmod(ws.root / "data" / "sub", 0o700)

    res = ws.run("push", "data", expect_rc=0)

    assert "upload:" not in res.out
    assert '"mode":"40700"' in _manifest_body(ws, "data")
    assert ws.run("status", "data", expect_rc=0).out.strip() == ""


def test_push_ignores_symlink_permission_drift(ws):
    ws.write("data/a.txt", "a")
    os.symlink("a.txt", ws.root / "data" / "link")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    # Simulate a manifest written where symlinks carry different permission
    # bits (e.g. macOS): flip the link record's perm bits on S3; the local
    # lstat cannot drift here (Linux has no lchmod).
    patched = []
    for line in _manifest_body(ws, "data").splitlines():
        obj = json.loads(line)
        if obj.get("link") is not None:
            obj["mode"] = "120700" if obj["mode"] != "120700" else "120755"
        patched.append(json.dumps(obj, separators=(",", ":")))
    key = f"{ws.prefix}/data-manifest.jsonl"
    ws.s3.put_object(Bucket=ws.bucket, Key=key, Body=("\n".join(patched) + "\n").encode())

    res = ws.run("push", "data", expect_rc=0)

    assert "Updating" not in res.err  # symlink perm bits are never compared


@pytest.mark.skipif(not SYMLINK_MTIME_SUPPORTED, reason=_NO_SYMLINK_MTIME_REASON)
def test_push_tracks_symlink_own_mtime_drift(ws):
    # A symlink's own mtime (not its target) drifting is a real change to
    # track, same as a directory's or special file's own mtime.
    ws.write("data/a.txt", "a")
    link = ws.root / "data" / "link"
    os.symlink("a.txt", link)
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    os.utime(link, ns=(_DRIFTED_LINK_MTIME_NS, _DRIFTED_LINK_MTIME_NS), follow_symlinks=False)

    res = ws.run("push", "data", expect_rc=0)

    assert "upload:" not in res.out  # an own-mtime drift re-transfers no data
    assert "Updating" in res.err
    assert f'"mtime_ns":{_DRIFTED_LINK_MTIME_NS}' in _manifest_body(ws, "data")
    assert ws.run("status", "data", expect_rc=0).out.strip() == ""


@pytest.mark.skipif(not SYMLINK_MTIME_SUPPORTED, reason=_NO_SYMLINK_MTIME_REASON)
def test_pull_restores_symlink_own_mtime_and_settles(ws):
    # status has no -o/--output (it always reads the entry's configured
    # path), so the empty destination here is that same configured path,
    # wiped first - the pull then restores into it from nothing.
    ws.write("data/a.txt", "a")
    link = ws.root / "data" / "link"
    os.symlink("a.txt", link)
    os.utime(link, ns=(_DRIFTED_LINK_MTIME_NS, _DRIFTED_LINK_MTIME_NS), follow_symlinks=False)
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    shutil.rmtree(ws.root / "data")

    ws.run("pull", "data", expect_rc=0)

    restored = os.lstat(ws.root / "data" / "link")
    assert restored.st_mtime_ns == _DRIFTED_LINK_MTIME_NS

    res = ws.run("status", "data", expect_rc=0)
    assert res.out.strip() == ""


def test_status_reports_directory_mtime_drift(ws):
    # Creating and then removing a file inside a directory bumps the
    # directory's own mtime - a real drift status now reports, no longer
    # suppressed.
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}}, mtime_window=0)
    ws.run("push", "data", expect_rc=0)

    tmp = ws.root / "data" / "tmp.txt"
    tmp.write_text("x")
    tmp.unlink()

    res = ws.run("status", "data", expect_rc=0)
    assert any(ln.startswith("M") and "mtime" in ln for ln in res.out.splitlines())


def test_push_tracks_directory_mtime_drift(ws):
    # A directory's own mtime drifting (children added/removed since the last
    # push) is a real change to track, same as a symlink's or special file's.
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}}, mtime_window=0)
    ws.run("push", "data", expect_rc=0)

    tmp = ws.root / "data" / "tmp.txt"
    tmp.write_text("x")
    tmp.unlink()
    actual_ns = os.lstat(ws.root / "data").st_mtime_ns

    res = ws.run("push", "data", expect_rc=0)

    assert "upload:" not in res.out  # a directory mtime drift re-transfers no data
    assert "Updating" in res.err
    body = _manifest_body(ws, "data")
    record = next(
        json.loads(line) for line in body.splitlines()[1:] if json.loads(line)["path"] == "."
    )
    assert record["mtime_ns"] == actual_ns  # the drifted mtime is now recorded
    assert ws.run("status", "data", expect_rc=0).out.strip() == ""  # no perpetual M


def test_pull_restores_directory_mtime_and_settles(ws):
    # status has no -o/--output (it always reads the entry's configured
    # path), so the empty destination here is that same configured path,
    # wiped first - the pull then restores into it from nothing.
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}}, mtime_window=0)
    ws.run("push", "data", expect_rc=0)

    tmp = ws.root / "data" / "tmp.txt"
    tmp.write_text("x")
    tmp.unlink()
    ws.run("push", "data", expect_rc=0)  # records the drifted directory mtime
    recorded_ns = os.lstat(ws.root / "data").st_mtime_ns

    shutil.rmtree(ws.root / "data")

    ws.run("pull", "data", expect_rc=0)

    assert os.lstat(ws.root / "data").st_mtime_ns == recorded_ns

    res = ws.run("status", "data", expect_rc=0)
    assert res.out.strip() == ""


def test_pull_settles_every_directory_through_a_deep_nested_tree(ws):
    # Coverage for apply_manifest's ancestor stack across several nesting
    # levels open at once, not just one directory deep: every level's own
    # mode and mtime must converge, from the leaf back up to the root.
    ws.write("data/a.txt", "a")
    ws.write("data/l1/b.txt", "b")
    ws.write("data/l1/l2/c.txt", "c")
    ws.write("data/l1/l2/l3/d.txt", "d")
    levels = (".", "l1", "l1/l2", "l1/l2/l3")
    for rel, mode in zip(levels, (0o750, 0o751, 0o752, 0o753), strict=True):
        os.chmod(ws.root / "data" / rel, mode)
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    recorded = {rel: os.lstat(ws.root / "data" / rel) for rel in levels}

    dest = ws.root / "restore"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)

    for rel, src_st in recorded.items():
        dst_st = os.lstat(dest if rel == "." else dest / rel)
        assert dst_st.st_mode & 0o777 == src_st.st_mode & 0o777
        assert dst_st.st_mtime_ns == src_st.st_mtime_ns


def test_push_skips_directory_mtime_drift_within_window(ws):
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}}, mtime_window=2)
    ws.run("push", "data", expect_rc=0)

    ns = os.lstat(ws.root / "data").st_mtime_ns + 1_000_000_000  # +1s: inside the 2s window
    os.utime(ws.root / "data", ns=(ns, ns))

    res = ws.run("push", "data", expect_rc=0)
    assert "Updating" not in res.err  # inside the window: no journal event


def test_single_file_push_refreshes_manifest_on_mode_change(ws):
    local = ws.write("solo.txt", "content")
    os.chmod(local, 0o644)
    ws.config({"solo": {"path": str(local)}})
    ws.run("push", "solo", expect_rc=0)
    os.chmod(local, 0o600)

    res = ws.run("push", "solo", expect_rc=0)

    assert "upload:" not in res.out
    assert '"mode":"100600"' in _manifest_body(ws, "solo")
    assert ws.run("status", "solo", expect_rc=0).out.strip() == ""
    res = ws.run("push", "solo", expect_rc=0)
    assert "Updating" not in res.err


def test_single_file_push_checksum_refreshes_manifest_on_mode_change(ws):
    local = ws.write("solo.txt", "content")
    os.chmod(local, 0o644)
    ws.config({"solo": {"path": str(local)}})
    ws.run("push", "solo", expect_rc=0)
    os.chmod(local, 0o600)

    res = ws.run("push", "--checksum", "solo", expect_rc=0)

    assert "upload:" not in res.out  # manifest-only refresh, no re-upload
    assert '"mode":"100600"' in _manifest_body(ws, "solo")


def test_push_refreshes_manifest_on_entry_root_mode_change(ws):
    ws.write("data/a.txt", "alpha")
    os.chmod(ws.root / "data", 0o755)
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    os.chmod(ws.root / "data", 0o700)

    ws.run("push", "data", expect_rc=0)

    assert '"path":".","mode":"40700"' in _manifest_body(ws, "data")
    assert ws.run("status", "data", expect_rc=0).out.strip() == ""


def test_push_dry_run_previews_mode_only_manifest_refresh(ws):
    p = ws.write("data/a.txt", "alpha")
    os.chmod(p, 0o644)
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    os.chmod(p, 0o600)

    res = ws.run("push", "--dry-run", "data", expect_rc=0)

    assert "would update manifest" in res.out
    assert "upload:" not in res.out
    assert '"mode":"100600"' not in _manifest_body(ws, "data")  # changed nothing


def test_checksum_push_writes_missing_manifest_for_existing_single_object(ws):
    local = ws.write("solo.txt", "same")
    ws.config({"solo": {"path": str(local)}})
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/solo", Body=b"same")

    ws.run("push", "--checksum", "solo", expect_rc=0)

    assert "solo-manifest.jsonl" in ws.keys()


def test_ls_remote_lists_entry(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("ls-remote", expect_rc=0)
    assert "data" in res.out.split()


def test_ls_remote_entry_lists_manifest_files(ws):
    ws.write("data/a.txt", "x")
    ws.write("data/sub/b.txt", "y")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("ls-remote", "data", expect_rc=0)
    assert "a.txt" in res.out
    assert "b.txt" in res.out


def test_ls_remote_subpath_lists_files(ws):
    ws.write("data/sub/b.txt", "y")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("ls-remote", str(ws.root / "data" / "sub"), expect_rc=0)
    assert "b.txt" in res.out


def test_pull_is_noop_when_local_already_matches(ws):
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    # Pulling back onto the matching tree hits the early "nothing to do" path.
    res = ws.run("pull", "data", expect_rc=0)
    assert res.out.strip() == ""
    assert (ws.root / "data" / "a.txt").read_text() == "a"


def test_pull_unpushed_entry_reports_not_found(ws):
    # download_manifest -> get_object hits NotFoundError for a never-pushed
    # entry, which must still map to a clean "not found" (not a crash).
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    res = ws.run("pull", "data", "-o", str(ws.root / "out"))
    assert res.rc != 0
    assert "not found" in (res.err + res.out).lower()


def test_ls_remote_unpushed_entry_reports_not_found(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    res = ws.run("ls-remote", "data")
    assert res.rc == 1
    assert "not found on s3" in res.err.lower()


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


def test_single_file_pull_replaces_symlink_destination(ws):
    # Restoring a regular file whose destination is a symlink must replace the
    # link with the file, never write through it into the link's target.
    f = ws.write("solo.txt", "backup-content")
    ws.config({"solo.txt": {"path": str(f)}})
    ws.run("push", "solo.txt", expect_rc=0)

    victim = ws.write("victim.txt", "do-not-touch")
    dest = ws.root / "out.txt"
    os.symlink(victim, dest)  # dest is a symlink -> victim

    ws.run("pull", "solo.txt", "-o", str(dest), expect_rc=0)

    assert not dest.is_symlink()
    assert dest.read_text() == "backup-content"
    assert victim.read_text() == "do-not-touch"  # link target untouched


def test_push_with_unreadable_file_warns_and_exits_2(ws, monkeypatch):
    # A skipped (unreadable) file is a WARNED outcome: the readable files still
    # upload and the manifest still updates, but the run exits 2 so an incomplete
    # backup is detectable. Exercised via run() (which maps warnings to exit 2).
    import signal

    from s3bak import cli

    ws.write("data/good.txt", "good")
    bad = ws.write("data/bad.txt", "secret")
    os.chmod(bad, 0)
    ws.config({"data": {"path": str(ws.root / "data")}})

    monkeypatch.setattr("sys.argv", ["s3bak", "push", "data"])
    saved = signal.getsignal(signal.SIGINT)
    try:
        rc = cli.run()
    finally:
        signal.signal(signal.SIGINT, saved)
        os.chmod(bad, 0o644)

    assert rc == 2
    assert "data/good.txt" in ws.keys()
    assert "data/bad.txt" not in ws.keys()


def test_push_broken_pipe_from_transfer_output_maps_to_141(ws, monkeypatch):
    # F1 regression: on_result runs as s3transfer's "done" callback, and
    # s3transfer's own futures.py wraps that callback in a bare `except
    # Exception` that only logs - it never re-raises. Without _transfer's own
    # catch/reraise, a closed stdout during a transfer (`s3bak push data |
    # head -n 0`) would make the result line's BrokenPipeError vanish inside
    # s3transfer instead of surfacing: the run would look like a clean
    # success (exit 0) instead of the documented exit 141, like the
    # sigpipe-mapping tests below (test_run_diff_maps_sigpipe_to_broken_pipe
    # in test_status_diff.py) already cover for the diff child process.
    import signal

    from s3bak import cli
    from s3bak.console import console

    ws.write("data/a.txt", "alpha")
    ws.write("data/b.txt", "beta")
    ws.config({"data": {"path": str(ws.root / "data")}})

    def broken_out(text: str) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(console, "out", broken_out)
    monkeypatch.setattr("sys.argv", ["s3bak", "push", "data"])
    saved = signal.getsignal(signal.SIGINT)
    try:
        rc = cli.run()
    finally:
        signal.signal(signal.SIGINT, saved)

    assert rc == 141
    # The uploads themselves ran to completion before their result line's
    # print discovered the broken pipe - only the reporting was lost, not the
    # transfer.
    assert "data/a.txt" in ws.keys()
    assert "data/b.txt" in ws.keys()


def test_pull_delete_removes_extra_symlink_to_dir(ws):
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "restore"
    dest.mkdir()
    (dest / "keep.txt").write_text("k")
    (dest / "realtarget").mkdir()
    os.symlink("realtarget", dest / "extralink")  # an extra symlink-to-dir

    ws.run("pull", "data", "-o", str(dest), "--delete", "--yes", expect_rc=0)
    assert not os.path.lexists(dest / "extralink")  # unlinked, not rmdir-skipped
    assert (dest / "keep.txt").read_text() == "k"


def test_push_after_local_delete_keeps_backup_by_default(ws):
    # Deleting is never the default: the object AND its manifest record both
    # survive the next push, status reports the file as D, and a pull would
    # still restore it. Only push --delete (confirmed) removes backups.
    ws.write("data/a.txt", "a")
    ws.write("data/b.txt", "b")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    assert "data/b.txt" in ws.keys()

    (ws.root / "data" / "b.txt").unlink()
    (ws.root / "data" / "a.txt").write_text("a-changed")  # forces a manifest rewrite
    res = ws.run("push", "data", expect_rc=0)

    assert "delete:" not in res.out
    assert "data/b.txt" in ws.keys()
    assert "data/a.txt" in ws.keys()
    manifest_body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")[
        "Body"
    ].read()
    assert b'"path":"./b.txt"' in manifest_body
    res = ws.run("status", "data", expect_rc=0)
    assert f"D {ws.root / 'data' / 'b.txt'}" in res.out

    dest = ws.root / "restore"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert (dest / "b.txt").read_text() == "b"


def test_push_with_only_a_local_delete_settles_directory_mtime_but_keeps_the_record(ws):
    # Nothing to transfer and the kept file record is not itself a structural
    # change - but the deletion bumped the directory's own mtime, which push
    # now tracks and refreshes; a second push (nothing left to see) converges.
    ws.write("data/a.txt", "a")
    ws.write("data/b.txt", "b")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "b.txt").unlink()
    first = ws.run("push", "data", expect_rc=0)
    second = ws.run("push", "data", expect_rc=0)

    assert first.out == ""
    assert second.out == ""
    assert "Updating" in first.err  # the directory's own mtime drifted
    assert "Updating" not in second.err  # settled: converges
    assert (
        b'"path":"./b.txt"'
        in ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")["Body"].read()
    )  # the file record survives (no --delete)


def test_push_keeps_records_of_locally_deleted_symlink_and_empty_dir(ws):
    ws.write("data/a.txt", "a")
    (ws.root / "data" / "emptydir").mkdir()
    os.symlink("a.txt", ws.root / "data" / "link")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "link").unlink()
    (ws.root / "data" / "emptydir").rmdir()
    (ws.root / "data" / "a.txt").write_text("changed")  # forces a manifest rewrite
    ws.run("push", "data", expect_rc=0)

    manifest_body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")[
        "Body"
    ].read()
    assert b'"path":"./link"' in manifest_body
    assert b'"path":"./emptydir"' in manifest_body


def test_push_warns_when_a_kept_subtree_ends_up_under_a_file(ws):
    # dir -> file replacement while the old records are kept: the manifest can
    # no longer restore as a tree, which the default (no-prompt) push must
    # surface as a warning.
    ws.write("data/d/x.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    shutil.rmtree(ws.root / "data" / "d")
    (ws.root / "data" / "d").write_text("now a file")
    res = ws.run("push", "data", expect_rc=0)

    assert "non-directory" in res.err
    assert "./d" in res.err


def test_push_dry_run_previews_the_kept_subtree_warning(ws):
    # The dry run runs the manifest merge for real (upload skipped), so the
    # same structural warning surfaces during the rehearsal.
    ws.write("data/d/x.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    shutil.rmtree(ws.root / "data" / "d")
    (ws.root / "data" / "d").write_text("now a file")
    res = ws.run("push", "--dry-run", "data", expect_rc=0)

    assert "non-directory" in res.err
    assert "data/d" not in ws.keys()  # the conflicting file was not uploaded


def test_single_file_pull_warns_when_object_is_gone(ws):
    # The single-file lane matches the directory sync: a missing object
    # downloads nothing, and the stale record is warned about and skipped.
    target = ws.write("solo.conf", "cfg")
    ws.config({"solo.conf": {"path": str(target)}})
    ws.run("push", "solo.conf", expect_rc=0)
    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/solo.conf")

    dest = ws.root / "restored.conf"
    res = ws.run("pull", "solo.conf", "-o", str(dest), expect_rc=0)

    assert "a push retires the stale record" in res.err
    assert not dest.exists()


def test_single_file_pull_leaves_a_diverged_local_copy_untouched(ws):
    # Object gone AND the local copy diverged - here at the SAME size, the
    # case a size check could never catch: the record is skipped in full, so
    # the local content, mode, and mtime stay exactly as they were. Stamping
    # the record's mtime over the divergence would hide it from every later
    # size+mtime comparison while reporting a restore that never happened.
    target = ws.write("solo.conf", "cfg")
    ws.config({"solo.conf": {"path": str(target)}})
    ws.run("push", "solo.conf", expect_rc=0)

    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/solo.conf")
    target.write_text("xyz")  # same size, different content and mtime
    before = os.lstat(target)
    res = ws.run("pull", "solo.conf", expect_rc=0)

    assert "a push retires the stale record" in res.err
    assert target.read_text() == "xyz"
    after = os.lstat(target)
    assert (after.st_mode, after.st_mtime_ns) == (before.st_mode, before.st_mtime_ns)


def test_subpath_file_pull_warns_when_object_is_gone(ws):
    # The sub-path file lane takes the same skip: warn, restore nothing,
    # leave the local path alone.
    ws.write("data/a.txt", "alpha")
    ws.write("data/b.txt", "beta")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt")

    dest = ws.root / "restored.txt"
    res = ws.run("pull", "data/a.txt", "-o", str(dest), expect_rc=0)

    assert "a push retires the stale record" in res.err
    assert not dest.exists()


def test_pull_delete_removes_local_extras(ws):
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "restore"
    dest.mkdir()
    (dest / "extra.txt").write_text("e")
    ws.run("pull", "data", "-o", str(dest), "--delete", "--yes", expect_rc=0)

    assert (dest / "keep.txt").read_text() == "k"
    assert not (dest / "extra.txt").exists()


def test_pull_delete_removes_extra_directory_tree(ws):
    # Extra directories go too: children before parents (deepest-first), and
    # an empty extra directory is rmdir'd, not skipped.
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "restore"
    (dest / "extradir" / "sub").mkdir(parents=True)
    (dest / "extradir" / "sub" / "f.txt").write_text("x")
    (dest / "emptydir").mkdir()
    (dest / "keep.txt").write_text("k")

    ws.run("pull", "data", "-o", str(dest), "--delete", "--yes", expect_rc=0)
    assert not (dest / "extradir").exists()
    assert not (dest / "emptydir").exists()
    assert (dest / "keep.txt").read_text() == "k"


def test_pull_delete_removes_extras_in_post_order(ws):
    # Removal is streamed subtree by subtree in ascending S3-key order, not
    # one global deepest-first pass: extradir's children are removed before
    # extradir itself, and the whole extradir subtree finishes streaming
    # before the later sibling zzz.txt is even looked at.
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "restore"
    (dest / "extradir" / "sub").mkdir(parents=True)
    (dest / "extradir" / "sub" / "deep.txt").write_text("d")
    (dest / "keep.txt").write_text("k")
    (dest / "zzz.txt").write_text("z")

    res = ws.run("pull", "data", "-o", str(dest), "--delete", "--yes", expect_rc=0)

    deletes = [
        ln.removeprefix("delete: ") for ln in res.out.splitlines() if ln.startswith("delete: ")
    ]
    assert deletes == [
        str(dest / "extradir" / "sub" / "deep.txt"),
        str(dest / "extradir" / "sub"),
        str(dest / "extradir"),
        str(dest / "zzz.txt"),
    ]


def test_pull_delete_judges_a_leaf_extra_on_arrival_not_at_its_parents_close(ws):
    # A leaf extra is judged the moment it arrives in the ascending S3-key
    # stream - only a directory extra waits for its own subtree to finish.
    # "a/b.txt" sorts ahead of the whole "a/c" subtree, so it must be
    # reported before "a/c" is even opened; deferring every extra (leaf
    # included) to its parent directory's close would instead report
    # "d.txt, c, b.txt, e.txt, a", jumping b.txt behind a subtree it sorts
    # ahead of.
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "restore"
    (dest / "a" / "c").mkdir(parents=True)
    (dest / "a" / "b.txt").write_text("b")
    (dest / "a" / "c" / "d.txt").write_text("d")
    (dest / "a" / "e.txt").write_text("e")
    (dest / "keep.txt").write_text("k")

    res = ws.run("pull", "data", "-o", str(dest), "--delete", "--yes", expect_rc=0)

    deletes = [
        ln.removeprefix("delete: ") for ln in res.out.splitlines() if ln.startswith("delete: ")
    ]
    assert deletes == [
        str(dest / "a" / "b.txt"),
        str(dest / "a" / "c" / "d.txt"),
        str(dest / "a" / "c"),
        str(dest / "a" / "e.txt"),
        str(dest / "a"),
    ]


# --- pull --delete (confirmed removals) ----------------------------------------


def test_pull_delete_without_tty_keeps_extras_and_succeeds(ws):
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    extra = ws.write("data/extra.txt", "extra")

    res = ws.run("pull", "--delete", "data", expect_rc=0)

    assert "delete:" not in res.out
    assert "warning" not in res.err.lower()
    assert extra.exists()


def test_pull_delete_interactive_keeps_ancestors_of_kept_items(ws, answers):
    # Post-order prompting follows the ascending S3-key stream: "extra.txt"
    # sorts before the "extradir/" subtree (`.` < `d`), so it is asked first;
    # extradir/inner.txt is asked once the stream is inside that subtree, and
    # keeping it makes the closing extradir frame unremovable, so extradir is
    # kept without a prompt of its own.
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.write("data/extradir/inner.txt", "i")
    ws.write("data/extra.txt", "e")

    answers.feed("y", "n")  # delete extra.txt, keep extradir/inner.txt
    ws.run("pull", "--delete", "data", expect_rc=0)

    assert len(answers.prompts) == 2
    assert "extra.txt" in answers.prompts[0]
    assert "extradir" in answers.prompts[1] and "inner.txt" in answers.prompts[1]
    assert (ws.root / "data" / "extradir" / "inner.txt").exists()
    assert not (ws.root / "data" / "extra.txt").exists()


def test_pull_delete_interactive_rejecting_a_grandchild_keeps_every_open_ancestor(ws, answers):
    # Denying the deepest item never prompts for either directory still open
    # above it: keeping a child forces keeping its whole open ancestor chain,
    # not just its immediate parent.
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.write("data/extradir/child/grandchild.txt", "g")

    answers.feed("n")  # keep the grandchild
    ws.run("pull", "--delete", "data", expect_rc=0)

    assert len(answers.prompts) == 1
    assert "grandchild.txt" in answers.prompts[0]
    assert (ws.root / "data" / "extradir" / "child" / "grandchild.txt").exists()
    assert (ws.root / "data" / "extradir" / "child").is_dir()
    assert (ws.root / "data" / "extradir").is_dir()


def test_pull_delete_interactive_prompts_for_empty_extra_dir(ws, answers):
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    (ws.root / "data" / "emptydir").mkdir()

    answers.feed("y")
    ws.run("pull", "--delete", "data", expect_rc=0)

    assert len(answers.prompts) == 1
    assert not (ws.root / "data" / "emptydir").exists()


def test_pull_delete_interactive_q_aborts(ws, answers):
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.write("data/extra1.txt", "1")
    ws.write("data/extra2.txt", "2")

    answers.feed("y", "q")  # ascending order: extra1.txt then extra2.txt
    res = ws.run("pull", "--delete", "data")

    assert res.rc == 1
    assert "aborted" in res.err and "pull again to finish" in res.err
    assert not (ws.root / "data" / "extra1.txt").exists()
    assert (ws.root / "data" / "extra2.txt").exists()


def test_pull_delete_confirms_on_the_clean_tree_short_circuit_too(ws, answers):
    # A tree whose manifest already matches takes the pull short-circuit; its
    # --delete pass must run behind the same confirmation.
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.write("data/extra.txt", "e")

    answers.feed("n")
    ws.run("pull", "--delete", "data", expect_rc=0)

    assert len(answers.prompts) == 1
    assert (ws.root / "data" / "extra.txt").exists()


# --- data-safety guarantees: staged root replacement, delete gating, re-settle ---


def test_pull_keeps_conflicting_dir_root_when_object_is_gone(ws):
    # A single-file entry restoring over a local DIRECTORY, with the data
    # object gone from S3: nothing arrives in the stage, so there is nothing
    # to swap in - the directory (and its contents) survive, the stale
    # record is warned about, and the empty stage is retired.
    ws.write("data", "payload")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data")

    dest = ws.root / "out"
    keep = ws.write("out/precious.txt", "keep me")

    res = ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert "a push retires the stale record" in res.err
    assert keep.read_text() == "keep me"
    assert not list(ws.root.glob("*.s3bak-stage*"))


def test_pull_replaces_conflicting_dir_root_after_staged_download(ws):
    ws.write("data", "payload")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "out"
    ws.write("out/old.txt", "old")

    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert dest.read_text() == "payload"
    assert not list(ws.root.glob("*.s3bak-stage*"))


def test_pull_keeps_conflicting_file_root_when_dir_download_fails(ws, monkeypatch):
    # The directory counterpart: a file sitting at the restore root is only
    # replaced after the tree download succeeded.
    ws.write("data/a.txt", "alpha")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "out"
    dest.write_text("in the way")

    from s3bak.store import Boto3S3Store, TransferResult

    monkeypatch.setattr(
        Boto3S3Store,
        "sync_down",
        lambda self, *a, **k: TransferResult(returncode=1),
    )
    res = ws.run("pull", "data", "-o", str(dest))
    assert res.rc == 1
    assert dest.read_text() == "in the way"
    assert not list(ws.root.glob("*.s3bak-stage*"))


def test_pull_delete_skipped_when_metadata_apply_fails(ws):
    # The extras diff is only trustworthy on a tree in the recorded state: a
    # failed metadata apply (here: a recorded FIFO that no pull can create)
    # must skip the --delete pass instead of removing local extras.
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo unavailable")
    ws.write("data/a.txt", "alpha")
    os.mkfifo(ws.root / "data" / "pipe")
    ws.config({"data": {"path": str(ws.root / "data")}})
    res = ws.run("push", "data")
    assert res.rc in (0, 2)  # the special file itself is a sync warning

    dest = ws.root / "out"
    extra = ws.write("out/extra.txt", "x")

    res = ws.run("pull", "--delete", "--yes", "data", "-o", str(dest))
    assert res.rc == 1
    assert "skipping --delete" in res.err
    assert extra.exists()


def test_pull_delete_resettles_directory_mtime(ws):
    # Removing an extra bumps its parent directory's mtime AFTER the metadata
    # apply restored it; the mirror pull must leave the recorded mtime behind.
    ws.write("data/a.txt", "alpha")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "out"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    ws.write("out/extra.txt", "x")

    ws.run("pull", "--delete", "--yes", "data", "-o", str(dest), expect_rc=0)
    assert not (dest / "extra.txt").exists()
    assert os.lstat(dest).st_mtime_ns == os.lstat(ws.root / "data").st_mtime_ns


def test_pull_delete_short_circuit_resettles_directory_mtime(ws):
    # Same guarantee on the clean-tree short-circuit: only the extra differs.
    ws.write("data/a.txt", "alpha")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "out"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    recorded = os.lstat(ws.root / "data").st_mtime_ns
    ws.write("out/extra.txt", "x")
    os.utime(dest, ns=(recorded, recorded))  # back to matching: the short-circuit path

    ws.run("pull", "--delete", "--yes", "data", "-o", str(dest), expect_rc=0)
    assert not (dest / "extra.txt").exists()
    assert os.lstat(dest).st_mtime_ns == recorded


def test_single_file_push_repairs_size_drifted_object(ws):
    # An out-of-band overwrite leaves the object at the wrong size; the
    # single-file size+mtime check reads the head size and re-uploads.
    ws.write("data", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data", Body=b"x")

    res = ws.run("push", "data", expect_rc=0)
    assert "upload:" in res.out
    assert ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data")["Body"].read() == b"hello"


def test_push_delete_retires_strays_under_single_file_entry(ws):
    # Objects under entry/ are outside a file-shaped backup and invisible to
    # its sync; push --delete sweeps that listing explicitly.
    ws.write("data", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/rogue", Body=b"r")

    ws.run("push", "data", expect_rc=0)  # without --delete: kept
    assert "data/rogue" in ws.keys()

    res = ws.run("push", "--delete", "--yes", "data", expect_rc=0)
    assert "delete:" in res.out
    assert "data/rogue" not in ws.keys()
    ws.run("verify", "data", expect_rc=0)


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs an unreadable directory")
def test_push_delete_refuses_deletions_when_scan_is_incomplete(ws):
    # An unreadable local directory hides its files from the walk; their S3
    # objects would look like orphans. The delete lane must refuse them (and
    # keep their records) instead of mirroring a partial view.
    ws.write("data/a.txt", "a")
    ws.write("data/sub/f.txt", "f")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    sub = ws.root / "data" / "sub"
    os.chmod(sub, 0)
    try:
        res = ws.run("push", "--delete", "--yes", "data")
    finally:
        os.chmod(sub, 0o755)
    assert res.rc == 0  # cli.main; cli.run maps the warnings to exit 2
    assert "kept 1 deletion candidate(s)" in res.err
    assert "data/sub/f.txt" in ws.keys()
    assert "./sub/f.txt" in _manifest_body(ws, "data")


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs an unreadable directory")
def test_push_delete_refuses_deletions_when_incomplete_scan_path_looks_like_timestamp_warning(ws):
    # A path whose name contains the invalid-timestamp warning's text must not
    # let the "Skipping file .../invalid timestamp/f.txt. File/Directory is not
    # readable." gap warning be misread as the (harmless) timestamp fallback.
    # A substring match on the warning body would clear scan_incomplete and
    # mirror a partial view, deleting a good backup.
    ws.write("data/a.txt", "a")
    ws.write("data/invalid timestamp/f.txt", "f")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    sub = ws.root / "data" / "invalid timestamp"
    os.chmod(sub, 0)
    try:
        res = ws.run("push", "--delete", "--yes", "data")
    finally:
        os.chmod(sub, 0o755)
    assert res.rc == 0  # cli.main; cli.run maps the warnings to exit 2
    assert "kept 1 deletion candidate(s)" in res.err
    assert "data/invalid timestamp/f.txt" in ws.keys()
    assert "./invalid timestamp/f.txt" in _manifest_body(ws, "data")


def test_pull_errors_on_size_mismatched_object(ws):
    # An out-of-band overwrite leaves the S3 object a different size than the
    # manifest records. Pull must not apply metadata and report success on the
    # wrong bytes - restore fidelity is the point of the backup.
    ws.write("data", "hello")  # 5 bytes
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data", Body=b"x")  # 1 byte
    dest = ws.root / "restored"
    res = ws.run("pull", "data", "-o", str(dest))
    assert res.rc == 1
    assert "size does not match" in (res.out + res.err).lower()


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs an unsearchable directory")
def test_push_delete_refuses_when_sub_ancestor_is_unsearchable(ws):
    # os.path.lexists reports EACCES (an unsearchable parent) as "absent";
    # push --delete must not read that as "locally deleted" and drop a backup
    # for a path it merely could not reach.
    ws.write("data/locked/file.txt", "secret")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    locked = ws.root / "data" / "locked"
    os.chmod(locked, 0)
    try:
        res = ws.run("push", "--delete", "--yes", "data/locked/file.txt")
    finally:
        os.chmod(locked, 0o755)
    assert res.rc == 1
    assert "cannot access" in res.err.lower()
    assert "data/locked/file.txt" in ws.keys()  # the backup is intact


def test_subpath_pull_delete_keeps_excluded_local_file(ws):
    # A sub-path pull --delete's local walk must anchor the entry's excludes at
    # the ENTRY root; otherwise an excluded local-only file under the sub is
    # matched against the wrong prefix, seen as an extra, and deleted.
    ws.write("data/sub/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["sub/private/*"]}})
    ws.run("push", "data", expect_rc=0)

    secret = ws.write("data/sub/private/secret", "keep me")  # excluded, local-only
    ws.run("pull", "--delete", "--yes", "data/sub", expect_rc=0)
    assert secret.exists() and secret.read_text() == "keep me"  # the exclude protected it


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs an unreadable file")
def test_verify_checksum_warns_on_unreadable_local_file(ws):
    # A verification tool must say it could not check a file, not silently skip
    # it as if the content matched.
    p = ws.write("data", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    os.chmod(p, 0)
    try:
        res = ws.run("verify", "--checksum", "data")
    finally:
        os.chmod(p, 0o644)
    assert "cannot read local file for --checksum" in res.err
    assert "1 warning(s)" in res.out  # surfaced in the summary, not reported OK


def test_pull_detects_size_mismatched_object(ws):
    # The metadata apply must catch an object whose restored size no longer
    # matches the record (out-of-band overwrite): applying metadata over
    # wrong content would report a successful restore that is not one.
    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt", Body=b"x")  # 1 byte
    res = ws.run("pull", "data", "-o", str(ws.root / "out"))
    assert res.rc == 1
    assert "size does not match" in res.err.lower()


def test_pull_warns_and_skips_a_record_whose_object_is_gone(ws):
    # A record whose object is gone (an interrupted deletion, an out-of-band
    # delete) is stale residue, not a reason to abort a restore: the pull
    # warns, skips it, and restores everything else; the next push retires
    # the record. run() maps the warning to exit 2.
    ws.write("data/a.txt", "hello")
    ws.write("data/b.txt", "beta")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt")

    out = ws.root / "out"
    res = ws.run("pull", "data", "-o", str(out), expect_rc=0)

    assert "a push retires the stale record" in res.err
    assert (out / "b.txt").read_text() == "beta"  # the rest still restored
    assert not (out / "a.txt").exists()


def test_pull_delete_warns_once_per_stale_record(ws):
    # The --delete re-settle re-applies metadata after removals; it must not
    # repeat the stale-record warning the first apply already emitted.
    ws.write("data/a.txt", "alpha")
    ws.write("data/b.txt", "beta")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt")
    (ws.root / "data" / "a.txt").unlink()

    out = ws.root / "out"
    ws.run("pull", "data", "-o", str(out), expect_rc=0)  # restore b.txt
    ws.write("out/extra.txt", "x")  # an extra, so the re-settle runs
    res = ws.run("pull", "--delete", "--yes", "data", "-o", str(out), expect_rc=0)

    assert res.err.count("a push retires the stale record") == 1
    assert not (out / "extra.txt").exists()


def test_pull_stale_record_maps_to_exit_2(ws, monkeypatch):
    # Through the real entry point: the stale-record warning is a warning,
    # so a pull that only met residue exits 2, not 0 and not 1.
    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt")

    import signal

    monkeypatch.setattr("sys.argv", ["s3bak", "pull", "data", "-o", str(ws.root / "out")])
    saved = signal.getsignal(signal.SIGINT)
    try:
        rc = cli.run()
    finally:
        signal.signal(signal.SIGINT, saved)

    assert rc == 2


def test_staged_pull_preserves_old_root_when_apply_fails(ws):
    # A conflicting-type restore root is swapped only after the download; if the
    # post-swap metadata apply then fails (here a size-mismatched object), the
    # swapped-out old root must be preserved, not destroyed by the cleanup.
    ws.write("data/a.txt", "hello")  # 5 bytes
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt", Body=b"x")  # corrupt: 1 byte

    dest = ws.root / "restore_here"
    dest.write_text("precious pre-existing data")  # a conflicting type (regular file)
    res = ws.run("pull", "data", "-o", str(dest))
    assert res.rc == 1
    assert "preserved at" in res.err
    stages = list(ws.root.glob("restore_here.s3bak-stage*"))
    assert len(stages) == 1
    assert (stages[0] / "replaced").read_text() == "precious pre-existing data"


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs an unsearchable directory")
def test_pull_subpath_rejects_unsearchable_ancestor(ws):
    # A deepest ancestor that lstat's fine (a directory) but is itself
    # unsearchable (mode 000) must be caught by the guard up front, not surface
    # as a bare EACCES only when the download tries to write through it.
    ws.write("data/a/file.txt", "content")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    a = ws.root / "data" / "a"
    os.chmod(a, 0)
    try:
        res = ws.run("pull", "data/a/file.txt")
    finally:
        os.chmod(a, 0o755)
    assert res.rc == 1
    assert "cannot access" in res.err.lower()


def test_staged_pull_preserves_old_root_on_interrupt_right_after_move(ws, monkeypatch):
    # A SIGINT landing right after the old root is moved into the stage (before
    # the code could record where it went) must still preserve it, not let the
    # cleanup delete the only copy.
    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "restore_here"
    dest.write_text("precious pre-existing data")  # a conflicting type -> staged pull

    real_replace = os.replace

    def replace_then_interrupt(src, dst):
        real_replace(src, dst)  # perform the real move...
        if os.fspath(dst).endswith(os.sep + "replaced"):
            raise KeyboardInterrupt  # ...then interrupt, as a signal would mid-swap

    monkeypatch.setattr("s3bak.commands.os.replace", replace_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        ws.run("pull", "data", "-o", str(dest))

    stages = list(ws.root.glob("restore_here.s3bak-stage*"))
    assert len(stages) == 1
    assert (stages[0] / "replaced").read_text() == "precious pre-existing data"


def test_staged_pull_preserves_old_root_when_delete_step_fails(ws, monkeypatch):
    # The apply succeeds after the staged swap, but the --delete extras step
    # fails: the preserve check runs AFTER --delete, so the old root is still kept.
    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    # An unrecorded object: the staged download materializes it as a local extra.
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/extra.txt", Body=b"x")

    dest = ws.root / "restore_here"
    dest.write_text("precious pre-existing data")  # conflicting type -> staged pull

    real_remove = os.remove

    def remove_failing_on_extra(path):
        if os.fspath(path).endswith("extra.txt"):
            raise OSError("injected delete failure")
        return real_remove(path)

    monkeypatch.setattr("s3bak.restore.os.remove", remove_failing_on_extra)
    res = ws.run("pull", "--delete", "--yes", "data", "-o", str(dest))
    assert res.rc == 1
    assert "preserved at" in res.err
    stages = list(ws.root.glob("restore_here.s3bak-stage*"))
    assert len(stages) == 1
    assert (stages[0] / "replaced").read_text() == "precious pre-existing data"


def test_subpath_pull_delete_keeps_data_when_whole_sub_excluded(ws):
    # An exclude that targets the sub itself ("sub/*") excludes the whole sub;
    # a sub-path pull --delete must not treat the excluded contents as extras.
    ws.write("data/sub/kept.txt", "kept")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["sub/*"]}})
    precious = ws.write("data/sub/precious.txt", "keep me")  # excluded, local-only
    ws.run("pull", "--delete", "--yes", "data/sub", expect_rc=0)
    assert precious.exists() and precious.read_text() == "keep me"


def test_pull_output_trailing_slash_is_exact(ws):
    # -o is the exact destination: a trailing slash must NOT append the entry
    # name (that container behavior belongs to the configured path only).
    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "exact"
    ws.run("pull", "data", "-o", str(dest) + os.sep, expect_rc=0)
    assert (dest / "a.txt").read_text() == "hello"  # restored AT dest...
    assert not (dest / "data").exists()  # ...not dest/data


def test_diff_ignores_conflicting_unrecorded_orphans(ws):
    # Out-of-band uploads leave unrecorded objects; two that conflict (a file
    # and a directory shape at one path) cannot be materialized together by a
    # bulk prefix sync. diff must ignore unrecorded objects (download only
    # recorded files), not fail trying to download them all.
    (ws.root / "data").mkdir()
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)  # empty dir entry: manifest = root only

    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/foo", Body=b"i am a file")
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/foo/bar", Body=b"under a dir")

    res = ws.run("diff", "data")
    assert res.rc == 0  # nothing recorded, nothing local: no diff, not a download failure


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs an unreadable directory")
def test_status_warns_when_unreadable_dir_hides_local_file(ws):
    # An unreadable directory hides its children from the walk; status must warn
    # that it could not see the whole tree, not silently report a clean tree
    # while a local-only file sits behind the unreadable directory.
    ws.write("data/a.txt", "a")
    (ws.root / "data" / "locked").mkdir()
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "locked" / "extra.txt").write_text("hidden")  # local-only
    os.chmod(ws.root / "data" / "locked", 0)
    try:
        res = ws.run("status", "data")
    finally:
        os.chmod(ws.root / "data" / "locked", 0o755)
    assert "not readable" in res.err.lower()  # surfaced, not silently clean


def test_push_delete_removes_exact_root_object(ws):
    # An object at a directory entry's OWN key (out-of-band, or a former-file
    # residue) is invisible to the slash-bounded delete lane; push --delete must
    # still retire it (verify flags it as a root type conflict otherwise).
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data", Body=b"orphan")

    assert "data" in ws.keys()  # the exact-key orphan exists
    ws.run("push", "--delete", "--yes", "data", expect_rc=0)
    assert "data" not in ws.keys()  # retired


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs an unreadable parent")
def test_status_warns_on_inaccessible_single_file_entry(ws):
    # A single-file entry whose parent is unsearchable is inaccessible, not
    # missing: status must warn, not silently report D.
    (ws.root / "locked").mkdir()
    solo = ws.root / "locked" / "solo"
    solo.write_text("hi")
    ws.config({"solo": {"path": str(solo)}})
    ws.run("push", "solo", expect_rc=0)

    os.chmod(ws.root / "locked", 0)
    try:
        res = ws.run("status", "solo")
    finally:
        os.chmod(ws.root / "locked", 0o755)
    assert "cannot access" in res.err.lower()
    assert not res.out.strip().startswith("D ")  # not falsely reported missing


@pytest.mark.skipif(os.name == "nt", reason="needs os.mkfifo")
def test_push_refreshes_special_file_mtime(ws):
    # A special file's own mtime is meaningful: an out-of-window mtime drift
    # must refresh the manifest, so status settles and pull restores the
    # current mtime.
    ws.write("data/a.txt", "a")
    fifo = ws.root / "data" / "pipe"
    os.mkfifo(fifo)
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    os.utime(fifo, (2_000_000_000, 2_000_000_000))  # drift far, mode unchanged
    actual_ns = os.lstat(fifo).st_mtime_ns
    ws.run("push", "data", expect_rc=0)

    body = (
        ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")["Body"]
        .read()
        .decode()
    )
    record = next(
        json.loads(line) for line in body.splitlines()[1:] if json.loads(line)["path"] == "./pipe"
    )
    assert record["mtime_ns"] == actual_ns  # the drifted mtime is now recorded
    assert ws.run("status", "data", expect_rc=0).out.strip() == ""  # no perpetual M


def test_diff_empty_single_file_detects_missing_local(ws):
    # A missing local file is a difference even against a 0-byte backup, whose
    # content diff vs /dev/null shows nothing and used to exit 0.
    ws.write("solo", "")  # 0-byte single-file entry
    ws.config({"solo": {"path": str(ws.root / "solo")}})
    ws.run("push", "solo", expect_rc=0)
    os.remove(ws.root / "solo")

    res = ws.run("diff", "solo")
    assert res.rc == 1
    assert "missing" in res.out.lower()


def test_missing_subpath_delete_aborts_on_damaged_manifest_before_deleting(ws):
    # The manifest is downloaded and validated BEFORE the subtree deletion, so
    # a corrupt manifest aborts the push while the backup is still intact.
    ws.write("data/sub/f.txt", "f")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl", Body=b"garbage")
    shutil.rmtree(ws.root / "data" / "sub")

    res = ws.run("push", "--delete", "--yes", "data/sub")
    assert res.rc == 1
    assert "data/sub/f.txt" in ws.keys()


def test_pull_preserves_old_root_when_cutover_and_rollback_both_fail(ws, monkeypatch):
    # Worst case of the staged swap: the old root was moved aside, the new
    # root cannot be renamed in, and the rollback rename fails too. The stage
    # cleanup must then keep the directory that holds the only copy.
    ws.write("data", "payload")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "out"
    ws.write("out/precious.txt", "keep me")

    real_replace = os.replace

    def failing_replace(src, dst):
        # Fail every rename INTO the restore root: both the cutover and the
        # rollback target it. Everything else (the atomic manifest/object
        # downloads, moving the old root aside) proceeds normally.
        if os.fspath(dst) == str(dest):
            raise OSError("injected rename failure")
        return real_replace(src, dst)

    monkeypatch.setattr("s3bak.commands.os.replace", failing_replace)
    with pytest.raises(OSError, match="injected"):
        ws.run("pull", "data", "-o", str(dest))

    stages = list(ws.root.glob("*.s3bak-stage*"))
    assert len(stages) == 1  # preserved, not cleaned up
    assert (stages[0] / "replaced" / "precious.txt").read_text() == "keep me"


def test_push_delete_retires_object_shielded_by_a_kind_conflict(ws):
    # A pushed file replaced locally by a symlink occupies its key, so the S3
    # object forms an update pair instead of an orphan and the sync's delete
    # lane never sees it. push --delete offers it out-of-lane: the object
    # goes, the symlink record (already journaled) stays.
    ws.write("data/f.txt", "was a file")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "f.txt").unlink()
    os.symlink("elsewhere", ws.root / "data" / "f.txt")
    ws.run("push", "data", expect_rc=0)  # records the symlink; the object stays
    assert "data/f.txt" in ws.keys()
    assert '"link":"elsewhere"' in _manifest_body(ws, "data")

    ws.run("push", "--delete", "--yes", "data", expect_rc=0)
    assert "data/f.txt" not in ws.keys()
    assert '"link":"elsewhere"' in _manifest_body(ws, "data")  # the record survives


def test_noop_push_does_not_rewrite_the_manifest(ws):
    # The rewrite condition is "the journal is non-empty": a push that finds
    # nothing to do must not re-upload an identical manifest.
    ws.write("data/a.txt", "alpha")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    key = f"{ws.prefix}/data-manifest.jsonl"
    first = ws.s3.get_object(Bucket=ws.bucket, Key=key)
    res = ws.run("push", "data", expect_rc=0)
    assert res.out.strip() == ""
    second = ws.s3.get_object(Bucket=ws.bucket, Key=key)
    assert first["ETag"] == second["ETag"]
    assert first["LastModified"] == second["LastModified"]  # not re-uploaded


def test_noop_subpath_push_does_not_rewrite_the_manifest(ws):
    # A sub-path push journals like a whole-entry push: an unchanged sub-tree
    # produces no events, so nothing is rewritten and no hook would fire.
    ws.write("data/sub/f.txt", "f")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    key = f"{ws.prefix}/data-manifest.jsonl"
    first = ws.s3.get_object(Bucket=ws.bucket, Key=key)
    ws.run("push", "data/sub", expect_rc=0)
    second = ws.s3.get_object(Bucket=ws.bucket, Key=key)
    assert first["LastModified"] == second["LastModified"]


def test_push_delete_of_unrecorded_object_leaves_manifest_untouched(ws):
    # Deleting an object the manifest never recorded journals nothing: the
    # object goes, and the manifest is not rewritten (there is no record to
    # drop).
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/stray.bin", Body=b"x")
    key = f"{ws.prefix}/data-manifest.jsonl"
    first = ws.s3.get_object(Bucket=ws.bucket, Key=key)

    ws.run("push", "--delete", "--yes", "data", expect_rc=0)
    assert "data/stray.bin" not in ws.keys()
    second = ws.s3.get_object(Bucket=ws.bucket, Key=key)
    assert first["LastModified"] == second["LastModified"]


def test_push_probes_readability_only_on_transfer(ws):
    # Readability is probed only on files a lane decided to copy: a chmod-0
    # file whose content is already backed up passes the stat compare, so no
    # open is attempted - the mode drift refreshes just its record, with no
    # warning and rc 0 (the old per-file probe warn-skipped it every run).
    bad = ws.write("data/bad.txt", "secret")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    os.chmod(bad, 0)
    try:
        res = ws.run("push", "data", expect_rc=0)
    finally:
        os.chmod(bad, 0o644)
    assert "not readable" not in res.err
    assert "upload:" not in res.out  # no transfer, record-only refresh
    assert '"mode":"100000"' in _manifest_body(ws, "data")


def test_checksum_push_with_unreadable_file_warns_and_exits_2(ws, monkeypatch):
    # --checksum reads every paired file; an unreadable one must warn-skip
    # (exit 2) like the default compare, not abort the sync with AccessDenied.
    # The probe runs before the content comparison for exactly this reason.
    import signal

    from s3bak import cli

    ws.write("data/bad.txt", "secret")
    ws.write("data/good.txt", "good")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    bad = ws.root / "data" / "bad.txt"
    os.chmod(bad, 0)
    ws.write("data/good.txt", "good v2")
    monkeypatch.setattr("sys.argv", ["s3bak", "push", "--checksum", "data"])
    saved = signal.getsignal(signal.SIGINT)
    try:
        rc = cli.run()
    finally:
        signal.signal(signal.SIGINT, saved)
        os.chmod(bad, 0o644)

    assert rc == 2  # warned, not aborted
    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/good.txt")["Body"].read()
    assert body == b"good v2"  # the readable file still synced


def test_subpath_delete_keeps_kind_changed_record_at_sub_itself(ws):
    # entry/a/b was pushed as a file, then became a directory locally. The
    # sub sync's S3 listing is slash-bounded (a/b/), so the object a/b never
    # enters any lane - its record is not provably stale and must survive a
    # sub-path --delete (records travel with their objects). A directory-
    # level push --delete retires the pair.
    ws.write("data/a/b", "was a file")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "a" / "b").unlink()
    ws.write("data/a/b/inner.txt", "now a dir")

    res = ws.run("push", "--delete", "--yes", "data/a/b", expect_rc=0)
    assert "data/a/b" in ws.keys()  # the out-of-listing object survives
    body = _manifest_body(ws, "data")
    assert '{"path":"./a/b","mode":"100644"' in body  # its record too
    assert "./a/b/inner.txt" in body
    assert "non-directory ./a/b" in res.err  # the surviving pair is warned


def test_push_delete_without_tty_keeps_kind_conflict_object(ws):
    # A non-TTY --delete without --yes answers no to everything - the
    # out-of-lane kind-conflict candidates included.
    ws.write("data/f.txt", "was a file")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "f.txt").unlink()
    os.symlink("elsewhere", ws.root / "data" / "f.txt")
    ws.run("push", "--delete", "data", expect_rc=0)
    assert "data/f.txt" in ws.keys()  # kept: every answer is no


def test_push_delete_retires_every_kind_conflict_object_and_counts_them(ws, monkeypatch):
    # PushJournal spools kind-conflict delete candidates to disk instead of
    # holding a list (memory independent of tree size); with several
    # candidates in one run, every one of them must still be offered and
    # deleted - not just the first - and pending_object_deletes must behave
    # as a plain counter of what was spooled, not a list length.
    from s3bak import commands

    names = ("f1.txt", "f2.txt", "f3.txt")
    for name in names:
        ws.write(f"data/{name}", "was a file")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    for name in names:
        (ws.root / "data" / name).unlink()
        os.symlink("elsewhere", ws.root / "data" / name)
    ws.run("push", "data", expect_rc=0)  # records the symlinks; the objects stay
    for name in names:
        assert f"data/{name}" in ws.keys()

    seen_counts: list[int] = []
    real = commands._delete_conflict_objects

    def spy(cfg, entry, plan, journal, opts):
        seen_counts.append(journal.pending_object_deletes)
        return real(cfg, entry, plan, journal, opts)

    monkeypatch.setattr(commands, "_delete_conflict_objects", spy)
    ws.run("push", "--delete", "--yes", "data", expect_rc=0)

    assert seen_counts == [3]  # the counter matched all three spooled candidates
    for name in names:
        assert f"data/{name}" not in ws.keys()  # every shielded object retired
    body = _manifest_body(ws, "data")
    assert body.count('"link":"elsewhere"') == 3  # every symlink record survives


def test_pending_object_delete_spool_round_trips_a_newline_in_the_key(tmp_path):
    # The spool JSON-encodes each candidate rather than using a plain
    # delimiter, precisely so a key containing a newline round-trips whole
    # instead of splitting into two spool lines. A local tree with such a
    # name is fragile to build portably, so this exercises PushJournal's
    # spool directly - the escape hatch this class of test is meant to use.
    from s3bak import localwalk
    from s3bak.syncops import PushJournal

    walker = localwalk.sync_walker([])
    journal = PushJournal(str(tmp_path / "j.journal"), None, window_ns=0, walker=walker)
    assert journal.pending_object_deletes == 0  # unset: no spool created yet

    journal._record_pending_object_delete("a\nb.txt", True)
    journal._record_pending_object_delete("plain.txt", False)
    assert journal.pending_object_deletes == 2  # a counter, not a list length

    journal.close()
    assert list(journal.iter_pending_object_deletes()) == [
        ("a\nb.txt", True),
        ("plain.txt", False),
    ]
    assert list(journal.iter_pending_object_deletes()) == []  # spool closed, forgotten


def test_pull_checksum_skips_the_size_mtime_gate(ws, monkeypatch):
    # --checksum ignores the size+mtime gate, so a real --checksum pull must not
    # pay for the (potentially millions of lstats) gate walk under it.
    from s3bak import commands

    ws.write("data/a.txt", "alpha")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    dest = ws.root / "out"

    calls: list[int] = []
    real = commands._manifest_matches_local
    monkeypatch.setattr(
        commands,
        "_manifest_matches_local",
        lambda *a, **k: (calls.append(1), real(*a, **k))[1],
    )
    ws.run("pull", "data", "--checksum", "-o", str(dest), expect_rc=0)
    assert calls == []  # gate walk skipped under a real --checksum pull

    calls.clear()
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert calls  # a plain pull still runs the gate


def test_subpath_push_records_ancestor_directory_mtime_drift(ws):
    # A leaf sub-path push must record a drifted ancestor-directory mtime (adding
    # the child bumps it), not just a mode change - otherwise pull restores the
    # stale directory mtime (docs/journal.md).
    ws.write("data/sub/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.write("data/sub/b.txt", "b")
    subdir = ws.root / "data" / "sub"
    os.utime(subdir, (1_600_000_000, 1_600_000_000))  # a distinct ancestor mtime
    ws.run("push", "data/sub/b.txt", expect_rc=0)

    dest = ws.root / "out"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert os.stat(dest / "sub").st_mtime_ns == 1_600_000_000_000_000_000
