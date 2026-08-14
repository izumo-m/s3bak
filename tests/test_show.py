"""`show` prints a stored file, and explains itself when it cannot.

Only a regular file has a data object, so `show` fails on everything else -
in its own words. The manifest is what knows the difference, yet `show` is
also the one command that must keep working while a manifest is damaged
(tests/test_damaged_manifest.py), so it streams first and consults the
manifest only to explain a miss.
"""

from __future__ import annotations

import os


def _pushed(ws):
    ws.write("data/a.txt", "alpha")
    ws.write("data/sub/b.txt", "beta")
    os.symlink("a.txt", ws.root / "data" / "link")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    return ws


def test_show_prints_the_stored_bytes(ws):
    _pushed(ws)
    res = ws.run("show", "data/a.txt", expect_rc=0)
    assert res.out == "alpha"
    assert res.err == ""


def test_show_rejects_a_directory_entry(ws):
    _pushed(ws)
    res = ws.run("show", "data", expect_rc=1)
    assert "only a regular file can be shown, not a directory: data" in res.err
    assert "NoSuchKey" not in res.err  # the SDK's own text must not leak


def test_show_rejects_a_directory_sub_path(ws):
    _pushed(ws)
    res = ws.run("show", "data/sub", expect_rc=1)
    assert "only a regular file can be shown, not a directory: data/sub" in res.err


def test_show_rejects_a_symlink(ws):
    # A symlink is recorded, never stored as an object: its target is the
    # record, so there is nothing to print.
    _pushed(ws)
    res = ws.run("show", "data/link", expect_rc=1)
    assert "only a regular file can be shown, not a symlink: data/link" in res.err


def test_show_reports_a_path_the_backup_does_not_hold(ws):
    _pushed(ws)
    res = ws.run("show", "data/nope.txt", expect_rc=1)
    assert "not found on S3: data/nope.txt" in res.err


def test_show_reports_a_record_whose_object_is_gone(ws):
    # Recorded as a regular file, but the object is missing: the same stale
    # residue a pull skips over, named as such rather than as "not found".
    _pushed(ws)
    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt")

    res = ws.run("show", "data/a.txt", expect_rc=1)
    assert "no data object behind this record" in res.err
    assert "a push retires the stale record" in res.err
    assert "data/a.txt" in res.err


def test_show_rejects_a_sub_path_of_a_single_file_entry(ws):
    f = ws.write("solo.txt", "x")
    ws.config({"solo.txt": {"path": str(f)}})
    ws.run("push", "solo.txt", expect_rc=0)

    res = ws.run("show", "solo.txt/inner", expect_rc=1)
    assert "sub path not allowed for single-file entry: solo.txt" in res.err


def test_show_reports_an_entry_never_pushed(ws):
    ws.write("data/a.txt", "alpha")
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("show", "data/a.txt", expect_rc=1)
    assert "not found on S3: data/a.txt" in res.err


def test_show_prints_an_object_the_manifest_does_not_record(ws):
    # An unrecorded object (verify warns about one; push --delete decides its
    # fate) is exactly what an operator wants to look at before deciding.
    # `show` streams before it consults the manifest, so it can.
    _pushed(ws)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/stray.txt", Body=b"stray")

    res = ws.run("show", "data/stray.txt", expect_rc=0)
    assert res.out == "stray"


def test_show_falls_back_to_the_bare_fact_when_the_manifest_is_damaged(ws):
    # The explanation needs the manifest, and a damaged one cannot give it:
    # report the absence rather than the damage, and never a traceback.
    _pushed(ws)
    ws.s3.put_object(
        Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl", Body=b"not a manifest at all"
    )

    res = ws.run("show", "data/nope.txt", expect_rc=1)
    assert "not found on S3: data/nope.txt" in res.err
    assert "Traceback" not in res.err
