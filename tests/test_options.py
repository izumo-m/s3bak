"""Option coverage: --all, --meta-only, --data-only, --dryrun, --color."""

from __future__ import annotations

import os

from s3bak.cli import _resolve_use_color


def test_push_all_uploads_every_entry(ws):
    ws.write("d1/a.txt", "a")
    ws.write("d2/b.txt", "b")
    ws.config({"d1": {"path": str(ws.root / "d1")}, "d2": {"path": str(ws.root / "d2")}})

    ws.run("push", "--all", expect_rc=0)

    keys = ws.keys()
    assert {"d1/a.txt", "d2/b.txt", "d1-ls-l.txt", "d2-ls-l.txt"} <= keys


def test_status_all_is_clean_after_push_all(ws):
    ws.write("d1/a.txt", "a")
    ws.write("d2/b.txt", "b")
    ws.config({"d1": {"path": str(ws.root / "d1")}, "d2": {"path": str(ws.root / "d2")}})
    ws.run("push", "--all", expect_rc=0)

    res = ws.run("status", "--all", expect_rc=0)
    assert res.out.strip() == ""


def test_push_meta_only_updates_manifest_not_data(ws):
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.write("data/new.txt", "new")
    ws.run("push", "--meta-only", "data", expect_rc=0)

    assert "data/new.txt" not in ws.keys()  # data was not uploaded
    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-ls-l.txt")["Body"].read()
    assert "new.txt" in body.decode()  # but it is recorded in the manifest


def test_meta_only_records_mode_change_and_clears_status(ws):
    f = ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    os.chmod(f, 0o600)
    res = ws.run("status", "data", expect_rc=0)
    assert "mode" in res.out  # a plain push would not refresh this (sync ignores mode)

    ws.run("push", "--meta-only", "data", expect_rc=0)
    res = ws.run("status", "data", expect_rc=0)
    assert res.out.strip() == ""


def test_push_data_only_skips_manifest_refresh(ws):
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    before = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-ls-l.txt")["Body"].read()

    (ws.root / "data" / "a.txt").write_text("a-much-bigger-content")
    ws.run("push", "--data-only", "data", expect_rc=0)

    obj = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt")["Body"].read()
    assert obj == b"a-much-bigger-content"  # data was uploaded
    after = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-ls-l.txt")["Body"].read()
    assert before == after  # but the manifest was not rewritten


def test_push_dryrun_uploads_nothing(ws):
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("push", "--dryrun", "data", expect_rc=0)
    assert ws.keys() == set()  # nothing was actually uploaded
    assert "a.txt" in res.out  # the planned upload is reported


def test_resolve_use_color_modes(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert _resolve_use_color("always") is True
    assert _resolve_use_color("never") is False
    monkeypatch.setenv("NO_COLOR", "1")
    assert _resolve_use_color("auto") is False


def test_diff_color_always_emits_ansi(ws):
    ws.write("data/a.txt", "one\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "a.txt").write_text("two\n")  # content differs
    res = ws.run("diff", "--color=always", "data")
    assert "\x1b[" in res.out  # ANSI escape forwarded to the diff child


def test_diff_no_color_has_no_ansi(ws):
    ws.write("data/a.txt", "one\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "a.txt").write_text("two\n")
    res = ws.run("diff", "--no-color", "data")
    assert "\x1b[" not in res.out


def test_pull_all_restores_every_entry(ws):
    ws.write("d1/a.txt", "a")
    ws.write("d2/b.txt", "b")
    ws.config({"d1": {"path": str(ws.root / "d1")}, "d2": {"path": str(ws.root / "d2")}})
    ws.run("push", "--all", expect_rc=0)

    (ws.root / "d1" / "a.txt").unlink()
    (ws.root / "d2" / "b.txt").unlink()
    ws.run("pull", "--all", expect_rc=0)

    assert (ws.root / "d1" / "a.txt").read_text() == "a"
    assert (ws.root / "d2" / "b.txt").read_text() == "b"


def test_pull_meta_only_restores_mode_without_download(ws):
    f = ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    os.chmod(f, 0o640)
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "restore"
    dest.mkdir()
    (dest / "a.txt").write_text("a")  # content already matches
    os.chmod(dest / "a.txt", 0o600)  # but the mode is wrong
    ws.run("pull", "--meta-only", "data", "-o", str(dest), expect_rc=0)

    assert (os.stat(dest / "a.txt").st_mode & 0o777) == 0o640  # mode applied, no download


def test_pull_data_only_downloads_without_metadata(ws):
    f = ws.write("data/a.txt", "hello")
    old = 1_600_000_000
    os.utime(f, (old, old))
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "restore"
    ws.run("pull", "--data-only", "data", "-o", str(dest), expect_rc=0)

    assert (dest / "a.txt").read_text() == "hello"  # data downloaded
    assert int((dest / "a.txt").stat().st_mtime) != old  # mtime NOT restored


def test_push_single_file_dryrun_uploads_nothing(ws):
    f = ws.write("solo.txt", "x")
    ws.config({"solo.txt": {"path": str(f)}})

    ws.run("push", "--dryrun", "solo.txt", expect_rc=0)
    assert ws.keys() == set()


def test_push_single_file_dryrun_prints_upload_once(ws):
    # Regression: the single-file dryrun path printed the upload line twice -
    # once directly and once via the shared results writer.
    f = ws.write("solo.txt", "x")
    ws.config({"solo.txt": {"path": str(f)}})

    res = ws.run("push", "--dryrun", "solo.txt", expect_rc=0)
    uploads = [ln for ln in res.out.splitlines() if ln.startswith("(dryrun) upload:")]
    assert len(uploads) == 1


def test_push_git_entry_meta_only_skips_manifest(ws):
    ws.write("repo.git/HEAD", "ref")
    ws.config({"repo.git": {"path": str(ws.root / "repo.git")}})

    ws.run("push", "--meta-only", "repo.git", expect_rc=0)
    assert "repo.git-ls-l.txt" not in ws.keys()  # .git + --meta-only skips the manifest
