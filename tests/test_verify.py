"""verify behaviour: the manifest x S3-listing merge-join, the --checksum
content lane, and the --all top-level sweep."""

from __future__ import annotations

import os

import pytest


def _push_tree(ws, entry: str = "data") -> None:
    ws.write(f"{entry}/a.txt", "alpha")
    ws.write(f"{entry}/sub/b.txt", "beta")
    os.symlink("a.txt", ws.root / entry / "ln")
    (ws.root / entry / "empty").mkdir()
    ws.config({entry: {"path": str(ws.root / entry)}})
    ws.run("push", entry, expect_rc=0)


def test_verify_clean_tree_is_ok(ws):
    _push_tree(ws)
    res = ws.run("verify", "data", expect_rc=0)
    assert "data: OK (2 file record(s), 2 data object(s))" in res.out
    assert res.err == ""


def test_verify_missing_object_is_an_error(ws):
    _push_tree(ws)
    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/sub/b.txt")
    res = ws.run("verify", "data", expect_rc=1)
    assert "missing data object" in res.err and "sub/b.txt" in res.err
    assert "1 error(s)" in res.out


def test_verify_size_mismatch_is_an_error(ws):
    _push_tree(ws)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt", Body=b"out-of-band write")
    res = ws.run("verify", "data", expect_rc=1)
    assert "size mismatch" in res.err and "a.txt" in res.err
    assert "manifest 5" in res.err


def test_verify_unrecorded_object_warns(ws):
    _push_tree(ws)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/zzz.bin", Body=b"stray")
    res = ws.run("verify", "data", expect_rc=0)  # cli.main; cli.run maps warnings to 2
    assert "unrecorded object" in res.err and "zzz.bin" in res.err
    assert "1 warning(s)" in res.out


def test_verify_warnings_map_to_exit_2_via_run(ws, monkeypatch):
    import signal

    from s3bak import cli

    _push_tree(ws)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/zzz.bin", Body=b"stray")
    monkeypatch.setattr("sys.argv", ["s3bak", "verify", "data"])
    saved = signal.getsignal(signal.SIGINT)
    try:
        rc = cli.run()
    finally:
        signal.signal(signal.SIGINT, saved)
    assert rc == 2


def test_verify_folder_objects(ws):
    _push_tree(ws)
    # A zero-byte marker (here at a recorded directory's key) is a warning; a
    # '/'-terminated key carrying data can never restore and is an error.
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/sub/", Body=b"")
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/vault/", Body=b"payload")
    res = ws.run("verify", "data", expect_rc=1)
    assert "folder object: " in res.err and "data/sub/" in res.err
    assert "folder object with data" in res.err and "data/vault/" in res.err


def test_verify_type_conflict_symlink(ws):
    _push_tree(ws)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/ln", Body=b"shadow")
    res = ws.run("verify", "data", expect_rc=1)
    assert "type conflict" in res.err and "records a symlink" in res.err


def test_verify_type_conflict_directory(ws):
    _push_tree(ws)
    # "sub.txt" sorts between the object key "sub" and the record key "sub/"
    # ("." < "/"), so this also exercises the waiting-buffer path.
    ws.write("data/sub.txt", "sibling")
    ws.run("push", "data", expect_rc=0)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/sub", Body=b"was-a-file")
    res = ws.run("verify", "data", expect_rc=1)
    assert "type conflict" in res.err and "records a directory" in res.err


def test_verify_type_conflict_at_entry_root(ws):
    _push_tree(ws)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data", Body=b"was-a-file")
    res = ws.run("verify", "data", expect_rc=1)
    assert "type conflict" in res.err
    assert f"{ws.prefix}/data (manifest records a directory" in res.err


def test_verify_archived_storage_class_is_an_error(ws):
    _push_tree(ws)
    ws.s3.put_object(
        Bucket=ws.bucket,
        Key=f"{ws.prefix}/data/a.txt",
        Body=b"alpha",
        StorageClass="GLACIER",
    )
    res = ws.run("verify", "data", expect_rc=1)
    assert "storage class GLACIER blocks restore" in res.err and "a.txt" in res.err


def test_verify_checksum_flags_silent_divergence(ws):
    _push_tree(ws)
    target = ws.root / "data" / "a.txt"
    st = target.stat()
    target.write_text("gamma")  # same size as "alpha"
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))  # mtime-preserving edit

    # The size+mtime check cannot see it: the default verify stays clean...
    ws.run("verify", "data", expect_rc=0)
    # ...and only --checksum reports it, as the push-invisible kind.
    res = ws.run("verify", "--checksum", "data", expect_rc=1)
    assert "content differs but size+mtime match" in res.err
    assert "push --checksum" in res.err


def test_verify_checksum_reports_pending_change_without_failing(ws):
    _push_tree(ws)
    (ws.root / "data" / "a.txt").write_text("a longer edit, stat drifted")
    res = ws.run("verify", "--checksum", "data", expect_rc=0)
    assert "pending change" in res.out and "a.txt" in res.out
    assert "1 pending change(s)" in res.out
    assert res.err == ""


def test_verify_never_pushed_entry_is_an_error(ws):
    ws.write("data/a.txt", "alpha")
    ws.config({"data": {"path": str(ws.root / "data")}})
    res = ws.run("verify", "data", expect_rc=1)
    assert "no backup on S3" in res.err


def test_verify_data_without_manifest_is_an_error(ws):
    _push_tree(ws)
    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")
    res = ws.run("verify", "data", expect_rc=1)
    assert "no manifest records them" in res.err
    # The summary's object tally reflects the objects actually found, not 0.
    assert "2 data object(s)" in res.out


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs an unsearchable parent")
def test_verify_checksum_warns_when_local_is_unreadable(ws):
    # verify --checksum must not report OK for a file whose content it could not
    # read (an unreadable ancestor); it warns instead of silently passing.
    ws.write("locked/data/a.txt", "alpha")
    ws.config({"data": {"path": str(ws.root / "locked" / "data")}})
    ws.run("push", "data", expect_rc=0)
    locked = ws.root / "locked"
    os.chmod(locked, 0o000)
    try:
        res = ws.run("verify", "data", "--checksum")
        assert "cannot read local file for --checksum" in res.err
        assert "OK (" not in res.out  # not a clean pass
    finally:
        os.chmod(locked, 0o755)


def test_verify_single_file_entry(ws):
    solo = ws.write("solo.txt", "content")
    ws.config({"solo.txt": {"path": str(solo)}})
    ws.run("push", "solo.txt", expect_rc=0)
    res = ws.run("verify", "solo.txt", expect_rc=0)
    assert "solo.txt: OK (1 file record(s), 1 data object(s))" in res.out

    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/solo.txt", Body=b"different length")
    res = ws.run("verify", "solo.txt", expect_rc=1)
    assert "size mismatch" in res.err


def test_verify_subpath_scopes_the_check(ws):
    _push_tree(ws)
    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt")
    # The damage is outside sub: verifying the sub-tree alone stays clean.
    ws.run("verify", "data/sub", expect_rc=0)
    res = ws.run("verify", "data", expect_rc=1)
    assert "missing data object" in res.err

    res = ws.run("verify", "data/nope")
    assert res.rc == 1
    assert "not found" in res.err


def test_verify_all_sweeps_the_top_level(ws):
    _push_tree(ws)
    ws.s3.put_object(
        Bucket=ws.bucket, Key=f"{ws.prefix}/ghost-manifest.jsonl", Body=b'{"s3bak_manifest":3}\n'
    )
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/stray/x.bin", Body=b"tree")
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/orphan.bin", Body=b"object")
    res = ws.run("verify", "--all", expect_rc=0)
    assert "data: OK" in res.out
    assert "stale manifest (no configured entry)" in res.err and "ghost" in res.err
    assert "data tree without a manifest" in res.err and "stray" in res.err
    assert "top-level object outside any configured entry" in res.err and "orphan.bin" in res.err


def test_verify_rejects_inapplicable_options(ws):
    _push_tree(ws)
    res = ws.run("verify", "--dry-run", "data")
    assert res.rc == 1 and "--dry-run only applies to" in res.err
    res = ws.run("verify", "--delete", "data")
    assert res.rc == 1 and "--delete only applies to" in res.err
    res = ws.run("verify", "--mtime-window", "1", "data")
    assert res.rc == 1 and "--mtime-window requires --checksum" in res.err


def test_verify_reports_strays_under_single_file_entry(ws):
    # A file-shaped manifest records nothing below entry/; verify sweeps that
    # listing so out-of-band uploads and type-change residue stay visible.
    ws.write("data", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/rogue", Body=b"r")

    res = ws.run("verify", "data", expect_rc=0)  # cli.main; cli.run maps warnings to 2
    assert "unrecorded object" in res.err and "data/rogue" in res.err
    assert "1 warning(s)" in res.out


def test_verify_reports_root_folder_object(ws):
    # A folder object at the tree's own key strips to an empty relative key;
    # it must still be classified as a folder object - with data it is an
    # error, since a '/'-terminated key cannot restore to any local path.
    _push_tree(ws)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/", Body=b"payload")
    res = ws.run("verify", "data", expect_rc=1)
    assert "folder object with data" in res.err

    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/", Body=b"")
    res = ws.run("verify", "data", expect_rc=0)  # cli.main; cli.run maps warnings to 2
    assert "folder object" in res.err
