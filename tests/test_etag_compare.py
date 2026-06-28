"""sync copies by content (ETag), not by size + last-modified.

s3bak passes ``boto3_s3.etagcompare.EtagComparison`` as the ``compare=``
strategy to every ``S3.sync`` (push and pull). These tests pin the two ways
that differs from the size+time default: a same-size, same-mtime content change
is still transferred, and an mtime-only change with identical bytes is not.
"""

from __future__ import annotations

import os
import stat


def test_push_reuploads_same_size_same_mtime_content_change(ws):
    # "hello" -> "world": identical length, and the mtime is restored, so the
    # size+last-modified default would skip it. ETag content comparison must
    # still re-upload because the bytes differ.
    p = ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    mtime = p.stat().st_mtime

    p.write_text("world")
    os.utime(p, (mtime, mtime))  # same size, same mtime, different content
    res = ws.run("push", "data", expect_rc=0)
    assert any("upload:" in ln and "a.txt" in ln for ln in res.out.splitlines())

    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt")["Body"].read()
    assert body == b"world"


def test_push_skips_reupload_on_mtime_only_change(ws):
    # Same content, but a far-future mtime - newer than the S3 object, so the
    # size+last-modified default would re-upload. ETag content comparison skips
    # it because the bytes are unchanged.
    p = ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    os.utime(p, (2_000_000_000, 2_000_000_000))  # year 2033, content untouched
    res = ws.run("push", "data", expect_rc=0)
    assert "upload:" not in res.out  # nothing re-uploaded


def test_single_file_push_reuploads_same_size_same_mtime_content_change(ws):
    # Single-file entry: same length, mtime restored, only content differs. The
    # old mtime gate would skip; needs_upload's ETag check re-uploads.
    f = ws.write("solo.txt", "hello")
    ws.config({"solo.txt": {"path": str(f)}})
    ws.run("push", "solo.txt", expect_rc=0)
    mtime = f.stat().st_mtime

    f.write_text("world")
    os.utime(f, (mtime, mtime))
    res = ws.run("push", "solo.txt", expect_rc=0)
    assert "upload:" in res.out

    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/solo.txt")["Body"].read()
    assert body == b"world"


def test_single_file_push_skips_reupload_on_mtime_only_change(ws):
    # Single-file entry: identical content, far-future mtime. The old mtime gate
    # would re-upload; the ETag check skips.
    f = ws.write("solo.txt", "hello")
    ws.config({"solo.txt": {"path": str(f)}})
    ws.run("push", "solo.txt", expect_rc=0)

    os.utime(f, (2_000_000_000, 2_000_000_000))
    res = ws.run("push", "solo.txt", expect_rc=0)
    assert "upload:" not in res.out


def test_pull_skips_download_when_content_matches(ws):
    # The destination already holds the right bytes but the wrong mode. The mode
    # mismatch defeats the "manifest already matches" short-circuit, so pull
    # reaches the sync; ETag content comparison then skips the download, and
    # apply_manifest still fixes the mode.
    p = ws.write("data/a.txt", "hello")
    os.chmod(p, 0o644)
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "out"
    dest.mkdir()
    (dest / "a.txt").write_text("hello")  # right content
    os.chmod(dest / "a.txt", 0o600)  # wrong mode

    res = ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert "download:" not in res.out  # content matched -> no download
    assert (dest / "a.txt").read_text() == "hello"
    assert stat.S_IMODE((dest / "a.txt").stat().st_mode) == 0o644  # manifest applied
