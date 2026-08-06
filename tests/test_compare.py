"""Sync copy decisions: the manifest size+mtime check by default, --checksum for content.

The default push/pull compare is ManifestFilter - size + mtime (within
mtime_window, default 10ms) against the manifest, reading no file content.
--checksum swaps in the ETag content comparison (reads every candidate file).
"""

from __future__ import annotations

import os


def _mtime_ns(p) -> int:
    return os.lstat(p).st_mtime_ns


# --- streaming merge-join (ManifestFilter reads the manifest once, in order) ---


def test_streaming_compare_handles_interleaved_dir_and_file(ws):
    # The manifest interleaves a directory marker ("foo/") between "foo.txt" and
    # "foo/bar" (S3 byte order: '.' < '/'). The streaming compare must merge-join
    # past the dir marker in lockstep with the sync's pairs, without desyncing.
    ws.write("data/foo.txt", "A")
    ws.write("data/foo/bar", "B")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "foo" / "bar").write_text("B-bigger")
    res = ws.run("push", "data", expect_rc=0)
    assert "foo/bar" in res.out
    assert "foo.txt" not in res.out  # unchanged sibling not re-uploaded

    # The reverse: modifying only foo.txt must not re-send foo/bar.
    (ws.root / "data" / "foo.txt").write_text("A-bigger")
    res = ws.run("push", "data", expect_rc=0)
    assert "foo.txt" in res.out
    assert "foo/bar" not in res.out


def test_streaming_compare_after_deleted_file_does_not_desync(ws):
    # A file in the manifest but deleted locally yields no source pair; the
    # cursor must skip its record and still match the keys that follow.
    ws.write("data/a.txt", "a")
    ws.write("data/m.txt", "m")
    ws.write("data/z.txt", "z")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "a.txt").unlink()  # no pair for "a.txt" (sorts first)
    (ws.root / "data" / "z.txt").write_text("z-bigger")  # changed (sorts last)
    res = ws.run("push", "data", expect_rc=0)
    assert "z.txt" in res.out  # changed file re-uploaded
    assert "m.txt" not in res.out  # unchanged middle file not re-uploaded


def test_streaming_compare_subpath(ws):
    # A sub-path sync strips the "sub/" prefix from both the pairs and the
    # manifest records; the streaming compare must stay aligned.
    ws.write("data/docs/a.txt", "a")
    ws.write("data/docs/b.txt", "b")
    ws.write("data/other.txt", "o")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "docs" / "b.txt").write_text("b-bigger")
    res = ws.run("push", str(ws.root / "data" / "docs"), expect_rc=0)
    assert "b.txt" in res.out
    assert "a.txt" not in res.out  # unchanged sibling under the sub-path


# --- default: size+mtime check -------------------------------------------------


def test_push_skips_when_nothing_changed(ws):
    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("push", "data", expect_rc=0)
    assert "upload:" not in res.out


def test_push_reuploads_on_mtime_drift_and_self_heals(ws):
    p = ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    os.utime(p, (2_000_000_000, 2_000_000_000))  # year 2033: outside any window
    res = ws.run("push", "data", expect_rc=0)
    assert "upload:" in res.out  # size+mtime check fails -> one spurious re-upload

    # ...which refreshed the manifest with the new mtime: self-healed.
    res = ws.run("push", "data", expect_rc=0)
    assert "upload:" not in res.out


def test_push_skips_mtime_drift_within_window(ws):
    p = ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}}, mtime_window=2)
    ws.run("push", "data", expect_rc=0)

    ns = _mtime_ns(p) + 1_000_000_000  # +1s: inside the configured 2s window
    os.utime(p, ns=(ns, ns))
    res = ws.run("push", "data", expect_rc=0)
    assert "upload:" not in res.out


def test_push_skips_mtime_drift_within_default_10ms_window(ws):
    # The default window (10ms) absorbs a sub-10ms drift (e.g. NTFS/exFAT
    # rounding on a restored mtime) so an unchanged file is not re-uploaded.
    p = ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})  # default 0.01s
    ws.run("push", "data", expect_rc=0)

    ns = _mtime_ns(p) + 5_000_000  # +5ms, inside the 10ms default
    os.utime(p, ns=(ns, ns))
    res = ws.run("push", "data", expect_rc=0)
    assert "upload:" not in res.out


def test_push_mtime_window_zero_is_strict(ws):
    p = ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}}, mtime_window=0)
    ws.run("push", "data", expect_rc=0)

    ns = _mtime_ns(p) + 1_000_000_000
    os.utime(p, ns=(ns, ns))
    res = ws.run("push", "data", expect_rc=0)
    assert "upload:" in res.out


def test_per_entry_mtime_window_zero_is_strict(ws):
    # A per-entry mtime_window=0 makes this entry strict even though the
    # top-level default is wide: a +1s drift is re-uploaded.
    p = ws.write("data/a.txt", "hello")
    ws.config(
        {"data": {"path": str(ws.root / "data"), "mtime_window": 0}},
        mtime_window=100,
    )
    ws.run("push", "data", expect_rc=0)

    ns = _mtime_ns(p) + 1_000_000_000  # +1s, inside the top-level 100s window
    os.utime(p, ns=(ns, ns))
    res = ws.run("push", "data", expect_rc=0)
    assert "upload:" in res.out  # per-entry 0 overrode the wide top-level window


def test_cli_mtime_window_flag_overrides_config(ws):
    # config sets a wide window; a +1s mtime drift is skipped by a plain push,
    # but --mtime-window 0 (strict) overrides the config value for that run and
    # re-uploads.
    p = ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}}, mtime_window=100)
    ws.run("push", "data", expect_rc=0)

    ns = _mtime_ns(p) + 1_000_000_000  # +1s, well inside the configured 100s window
    os.utime(p, ns=(ns, ns))

    res = ws.run("push", "data", expect_rc=0)  # config window -> skip
    assert "upload:" not in res.out

    res = ws.run("push", "--mtime-window", "0", "data", expect_rc=0)  # strict -> re-upload
    assert "upload:" in res.out


def test_cli_mtime_window_flag_affects_status(ws):
    # --mtime-window also tightens `status`, which shares the size+mtime-check
    # predicate: a +1s drift is quiet within a 2s window, reported at 0.
    p = ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}}, mtime_window=2)
    ws.run("push", "data", expect_rc=0)

    ns = _mtime_ns(p) + 1_000_000_000
    os.utime(p, ns=(ns, ns))

    assert ws.run("status", "data", expect_rc=0).out.strip() == ""  # within the 2s window
    res = ws.run("status", "--mtime-window", "0", "data", expect_rc=0)
    assert any("a.txt" in ln and ln.startswith("M") for ln in res.out.splitlines())


def test_push_misses_same_size_same_mtime_content_change(ws):
    # The size+mtime check's documented blind spot: identical size AND restored
    # mtime looks unchanged. --checksum exists for exactly this.
    p = ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ns = _mtime_ns(p)

    p.write_text("world")  # same length
    os.utime(p, ns=(ns, ns))  # mtime restored
    res = ws.run("push", "data", expect_rc=0)
    assert "upload:" not in res.out

    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt")["Body"].read()
    assert body == b"hello"  # S3 still holds the old bytes


def test_pull_downloads_remote_update(ws):
    src = ws.write("data/a.txt", "v1")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "out"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert (dest / "a.txt").read_text() == "v1"

    src.write_text("v2!!!")  # backup moves forward
    ws.run("push", "data", expect_rc=0)

    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert (dest / "a.txt").read_text() == "v2!!!"  # dest catches up


def test_pull_redownloads_on_local_mtime_drift_then_heals(ws):
    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "out"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)

    os.utime(dest / "a.txt", (2_000_000_000, 2_000_000_000))  # drift past the window
    res = ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert "download:" in res.out  # size+mtime check fails -> re-download

    # apply_manifest restored the recorded mtime, so the next pull is a no-op.
    res = ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert res.out.strip() == ""


# --- --checksum: content comparison --------------------------------------------


def test_push_checksum_catches_same_size_same_mtime_content_change(ws):
    p = ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ns = _mtime_ns(p)

    p.write_text("world")
    os.utime(p, ns=(ns, ns))
    res = ws.run("push", "--checksum", "data", expect_rc=0)
    assert "upload:" in res.out

    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt")["Body"].read()
    assert body == b"world"


def test_push_checksum_skips_mtime_only_change(ws):
    p = ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    os.utime(p, (2_000_000_000, 2_000_000_000))  # content untouched
    res = ws.run("push", "--checksum", "data", expect_rc=0)
    assert "upload:" not in res.out


def test_push_checksum_refreshes_stale_mtime_record(ws):
    # docs/sync.md's self-healing: an out-of-window mtime-only drift refreshes
    # the manifest even under --checksum (no re-transfer, since content is
    # equal), so status settles and a pull restores the current mtime instead
    # of a stale one. Without the refresh the file would show M forever.
    import json

    p = ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    os.utime(p, (2_000_000_000, 2_000_000_000))  # content untouched, mtime far off
    actual_ns = _mtime_ns(p)
    res = ws.run("push", "--checksum", "data", expect_rc=0)
    assert "upload:" not in res.out  # content unchanged: no re-transfer

    body = (
        ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")["Body"]
        .read()
        .decode()
    )
    record = next(
        json.loads(line) for line in body.splitlines()[1:] if json.loads(line)["path"] == "./a.txt"
    )
    assert record["mtime_ns"] == actual_ns  # the drifted mtime is now recorded

    assert ws.run("status", "data", expect_rc=0).out.strip() == ""  # no perpetual M


def test_single_file_push_checksum_refreshes_stale_mtime_record(ws):
    # The single-file counterpart of the self-healing refresh.
    import json

    p = ws.write("data", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    os.utime(p, (2_000_000_000, 2_000_000_000))
    actual_ns = _mtime_ns(p)
    res = ws.run("push", "--checksum", "data", expect_rc=0)
    assert "upload:" not in res.out

    body = (
        ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")["Body"]
        .read()
        .decode()
    )
    assert json.loads(body.splitlines()[1])["mtime_ns"] == actual_ns
    assert ws.run("status", "data", expect_rc=0).out.strip() == ""


def test_pull_checksum_repairs_same_size_same_mtime_corruption(ws):
    # The reason --checksum exists on pull: local content drifted while
    # keeping size and mtime, so the stat short-circuit must not swallow it.
    p = ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    st = os.lstat(p)
    p.write_text("HELLO")  # corrupt: same size
    os.utime(p, ns=(st.st_mtime_ns, st.st_mtime_ns))  # same mtime

    res = ws.run("pull", "--checksum", "data", expect_rc=0)
    assert "download:" in res.out
    assert p.read_text() == "hello"  # repaired


def test_pull_checksum_clean_tree_is_quiet(ws):
    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.run("pull", "data", expect_rc=0)  # settle metadata

    res = ws.run("pull", "--checksum", "data", expect_rc=0)
    assert res.out.strip() == ""  # verified clean: no downloads, no re-apply


def test_pull_checksum_skips_download_when_content_matches(ws):
    # The destination holds the right bytes but the wrong mode. The mode
    # mismatch defeats the "manifest already matches" short-circuit, so pull
    # reaches the sync; the ETag comparison skips the download, and
    # apply_manifest still fixes the mode.
    import stat

    p = ws.write("data/a.txt", "hello")
    os.chmod(p, 0o644)
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "out"
    dest.mkdir()
    (dest / "a.txt").write_text("hello")  # right content
    os.chmod(dest / "a.txt", 0o600)  # wrong mode

    res = ws.run("pull", "--checksum", "data", "-o", str(dest), expect_rc=0)
    assert "download:" not in res.out  # content matched -> no download
    assert stat.S_IMODE((dest / "a.txt").stat().st_mode) == 0o644  # manifest applied


def test_checksum_rejected_outside_push_pull(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    res = ws.run("status", "--checksum", "data")
    assert res.rc == 1
    assert "checksum" in res.err.lower()


# --- single-file entries --------------------------------------------------------


def test_single_file_push_size_mtime_check_and_self_heal(ws):
    f = ws.write("solo.txt", "hello")
    ws.config({"solo.txt": {"path": str(f)}})
    ws.run("push", "solo.txt", expect_rc=0)

    res = ws.run("push", "solo.txt", expect_rc=0)
    assert "upload:" not in res.out  # unchanged -> skip

    os.utime(f, (2_000_000_000, 2_000_000_000))
    res = ws.run("push", "solo.txt", expect_rc=0)
    assert "upload:" in res.out  # drift -> one re-upload

    res = ws.run("push", "solo.txt", expect_rc=0)
    assert "upload:" not in res.out  # manifest refreshed -> healed


def test_single_file_push_reuploads_when_object_missing(ws):
    # A manifest whose object is gone (an S3-side delete, an interrupted
    # deletion): a later plain push must notice the missing object
    # (head-object probe) and upload instead of trusting the record forever.
    f = ws.write("solo.txt", "x")
    ws.config({"solo.txt": {"path": str(f)}})
    ws.run("push", "solo.txt", expect_rc=0)
    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/solo.txt")

    res = ws.run("push", "solo.txt", expect_rc=0)
    assert "upload:" in res.out
    assert "solo.txt" in ws.keys()


def test_single_file_push_checksum_skips_mtime_only_change(ws):
    f = ws.write("solo.txt", "hello")
    ws.config({"solo.txt": {"path": str(f)}})
    ws.run("push", "solo.txt", expect_rc=0)

    os.utime(f, (2_000_000_000, 2_000_000_000))
    res = ws.run("push", "--checksum", "solo.txt", expect_rc=0)
    assert "upload:" not in res.out


def test_single_file_push_checksum_catches_content_change(ws):
    f = ws.write("solo.txt", "hello")
    ws.config({"solo.txt": {"path": str(f)}})
    ws.run("push", "solo.txt", expect_rc=0)
    ns = _mtime_ns(f)

    f.write_text("world")
    os.utime(f, ns=(ns, ns))
    res = ws.run("push", "--checksum", "solo.txt", expect_rc=0)
    assert "upload:" in res.out

    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/solo.txt")["Body"].read()
    assert body == b"world"
