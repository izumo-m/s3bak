"""Entry kinds: single-file entries, excludes, and `list`."""

from __future__ import annotations

import os
import shlex
import shutil

import pytest


def test_single_file_entry_roundtrip(ws):
    f = ws.write("solo.txt", "content\n")
    ws.config({"solo.txt": {"path": str(f)}})

    ws.run("push", "solo.txt", expect_rc=0)
    assert "solo.txt" in ws.keys()

    res = ws.run("status", "solo.txt", expect_rc=0)
    assert res.out.strip() == ""

    dest = ws.root / "out.txt"
    ws.run("pull", "solo.txt", "-o", str(dest), expect_rc=0)
    assert dest.read_text() == "content\n"


def test_excludes_skip_matching_files(ws):
    ws.write("data/keep.txt", "k")
    ws.write("data/skip.log", "s")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*.log"]}})

    ws.run("push", "data", expect_rc=0)

    keys = ws.keys()
    assert "data/keep.txt" in keys
    assert "data/skip.log" not in keys


def test_subpath_push_keeps_excludes_entry_rooted(ws):
    # "tmp/*" means <entry>/tmp - a sub-path push of build/ must NOT reanchor
    # it at build/tmp (that would drop build/tmp from the manifest and make a
    # later pull --delete remove never-excluded local files).
    ws.write("data/tmp/skip.txt", "s")  # excluded: entry-root tmp
    ws.write("data/build/tmp/keep.txt", "k")  # not excluded
    ws.write("data/build/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["tmp/*"]}})
    ws.run("push", "data", expect_rc=0)

    keys = ws.keys()
    assert "data/build/tmp/keep.txt" in keys
    assert "data/tmp/skip.txt" not in keys

    (ws.root / "data" / "build" / "tmp" / "keep.txt").write_text("k-v2")
    ws.run("push", str(ws.root / "data" / "build"), expect_rc=0)

    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/build/tmp/keep.txt")[
        "Body"
    ].read()
    assert body == b"k-v2"  # the sub sync did not exclude build/tmp
    mani = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")["Body"].read()
    assert "./build/tmp/keep.txt" in mani.decode()  # nor did the manifest patch
    res = ws.run("status", "data", expect_rc=0)
    assert res.out.strip() == ""


def test_entry_dir_replaced_by_same_stat_file_reuploads(ws):
    # The entry path was a directory; it becomes a single file whose stat
    # matches a record inside the stale dir manifest. The size+mtime check must
    # match records by rel (the basename), not by stat coincidence.
    import shutil

    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    st = os.lstat(ws.root / "data" / "a.txt")
    shutil.rmtree(ws.root / "data")
    f = ws.root / "data"
    f.write_text("hello")  # same size as the old ./a.txt record
    os.utime(f, ns=(st.st_mtime_ns, st.st_mtime_ns))  # and the same mtime

    res = ws.run("push", "data", expect_rc=0)
    assert "upload:" in res.out
    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data")["Body"].read()
    assert body == b"hello"


def test_first_subpath_push_keeps_dir_entry_shape(ws):
    # A sub-path push with no manifest on S3 yet creates one; the entry must
    # still classify as a directory entry afterwards (rel shape './...'), so
    # a whole-entry pull syncs the tree instead of cp-ing a bogus single file.
    ws.write("data/notes.txt", "n")
    ws.config({"data": {"path": str(ws.root / "data")}})

    ws.run("push", str(ws.root / "data" / "notes.txt"), expect_rc=0)

    dest = ws.root / "out"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert (dest / "notes.txt").read_text() == "n"


def test_push_file_subpath_uploads_and_keeps_status_clean(ws):
    ws.write("data/a.txt", "a")
    ws.write("data/sub/b.txt", "b")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "sub" / "b.txt").write_text("b-updated")
    ws.run("push", str(ws.root / "data" / "sub" / "b.txt"), expect_rc=0)

    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/sub/b.txt")["Body"].read()
    assert body == b"b-updated"
    res = ws.run("status", "data", expect_rc=0)
    assert res.out.strip() == ""  # the manifest subtree was patched too


def test_push_dir_subpath_uploads_new_file(ws):
    ws.write("data/keep.txt", "k")
    ws.write("data/sub/x.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "sub" / "y.txt").write_text("y")
    ws.run("push", str(ws.root / "data" / "sub"), expect_rc=0)

    assert "data/sub/y.txt" in ws.keys()
    res = ws.run("status", "data", expect_rc=0)
    assert res.out.strip() == ""


def test_symlinks_are_recorded_in_manifest_not_uploaded_as_data(ws):
    ws.write("data/real.txt", "real")
    ws.write("data/sub/x.txt", "insub")
    os.symlink("real.txt", ws.root / "data" / "link.txt")
    os.symlink("sub", ws.root / "data" / "linkdir")
    ws.config({"data": {"path": str(ws.root / "data")}})

    ws.run("push", "data", expect_rc=0)

    # symlinks must not be followed into data objects
    keys = ws.keys()
    assert "data/real.txt" in keys
    assert "data/sub/x.txt" in keys
    assert "data/link.txt" not in keys
    assert not any(k.startswith("data/linkdir/") for k in keys)

    # pull recreates them as symlinks from the manifest
    dest = ws.root / "restore"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert os.path.islink(dest / "link.txt")
    assert os.readlink(dest / "link.txt") == "real.txt"
    assert os.path.islink(dest / "linkdir")
    assert os.readlink(dest / "linkdir") == "sub"
    assert (dest / "real.txt").read_text() == "real"


def test_symlink_restore_replaces_existing_dir(ws):
    # Simulate an older follow-symlinks backup: the symlink path already holds a
    # real directory locally; restore must replace it with the symlink cleanly.
    ws.write("data/real.txt", "real")
    os.symlink("real.txt", ws.root / "data" / "link.txt")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "restore"
    (dest / "link.txt").mkdir(parents=True)
    (dest / "link.txt" / "stale.txt").write_text("stale")
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert os.path.islink(dest / "link.txt")
    assert os.readlink(dest / "link.txt") == "real.txt"


def test_empty_directory_subpath_pull_restores_a_directory(ws):
    ws.write("data/file.txt", "x")
    (ws.root / "data" / "empty").mkdir()
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    # The empty dir is in the manifest but has no S3 object; pulling it as a
    # sub-path must restore a directory, not fail as a missing single file.
    dest = ws.root / "out"
    ws.run("pull", str(ws.root / "data" / "empty"), "-o", str(dest), expect_rc=0)
    assert dest.is_dir()


def test_post_hook_failure_propagates(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data"), "post_hook": "exit 3"}})
    res = ws.run("push", "data")
    assert res.rc == 3


def test_post_hook_runs_on_success(ws):
    marker = ws.root / "hook-ran"
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data"), "post_hook": f"touch {marker}"}})
    ws.run("push", "data", expect_rc=0)
    assert marker.exists()


def test_windows_pull_applies_manifest_without_downloads(ws, monkeypatch):
    # On Windows, apply_manifest must run even when nothing was downloaded (an
    # empty-dir sub-path here): the restore must not be gated on sync_changed.
    # cmd_pull reads IS_WINDOWS from its own module, so patch it there.
    from s3bak import commands

    monkeypatch.setattr(commands, "IS_WINDOWS", True)
    ws.write("data/file.txt", "x")
    (ws.root / "data" / "empty").mkdir()
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "out"
    ws.run("pull", str(ws.root / "data" / "empty"), "-o", str(dest), expect_rc=0)
    assert dest.is_dir()


def test_symlink_entry_path_is_rejected(ws):
    (ws.root / "realdir").mkdir()
    os.symlink("realdir", ws.root / "linkentry")
    ws.config({"linkentry": {"path": str(ws.root / "linkentry")}})

    res = ws.run("push", "linkentry")
    assert res.rc != 0
    assert "symlink" in (res.err + res.out).lower()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo on this platform")
def test_special_file_entry_path_is_rejected(ws):
    fifo = ws.root / "fifo"
    os.mkfifo(fifo)
    ws.config({"fifo": {"path": str(fifo)}})

    res = ws.run("push", "fifo")
    assert res.rc != 0
    assert "regular file or directory" in (res.err + res.out).lower()


def test_inner_symlink_subpath_pull_restores_symlink(ws):
    # A symlink inside a dir entry, pulled as a sub-path, must come back as a
    # symlink (no data object exists for it).
    ws.write("data/real.txt", "r")
    os.symlink("real.txt", ws.root / "data" / "link.txt")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "out"
    ws.run("pull", str(ws.root / "data" / "link.txt"), "-o", str(dest), expect_rc=0)
    assert os.path.islink(dest)
    assert os.readlink(dest) == "real.txt"


def test_list_shows_configured_entries_without_constructing_s3_store(ws, monkeypatch):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    from s3bak import config

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("list must not construct an S3 store")

    monkeypatch.setattr(config, "Boto3S3Store", fail_if_called)

    res = ws.run("list", expect_rc=0)
    assert "data" in res.out


def test_entry_subpath_syntax_is_independent_of_cwd(ws, monkeypatch):
    ws.write("data/sub/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    elsewhere = ws.root / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    ws.run("push", "data/sub/a.txt", expect_rc=0)

    assert "data/sub/a.txt" in ws.keys()


def test_missing_subpath_push_delete_removes_only_that_s3_subtree(ws):
    ws.write("data/sub/a.txt", "a")
    ws.write("data/sub.txt", "sibling")
    ws.write("data/submarine/b.txt", "b")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    shutil.rmtree(ws.root / "data" / "sub")

    ws.run("push", "--delete", "data/sub", expect_rc=0)

    keys = ws.keys()
    assert not any(key == "data/sub" or key.startswith("data/sub/") for key in keys)
    assert "data/sub.txt" in keys
    assert "data/submarine/b.txt" in keys
    manifest_body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")[
        "Body"
    ].read()
    assert b'"path":"./sub"' not in manifest_body


def test_subpath_push_is_rejected_for_single_file_entry(ws):
    target = ws.write("single.txt", "x")
    ws.config({"single": {"path": str(target)}})

    res = ws.run("push", "--delete", "single/child")

    assert res.rc == 1
    assert "single-file entry" in res.err


def test_pre_hook_can_create_entry_target(ws):
    target = ws.root / "generated.txt"
    command = f"printf generated > {shlex.quote(str(target))}"
    ws.config({"generated": {"path": str(target), "pre_hook": command}})

    ws.run("push", "generated", expect_rc=0)

    assert target.read_text() == "generated"
    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/generated")["Body"].read()
    assert body == b"generated"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo on this platform")
def test_special_file_subpath_is_rejected(ws):
    (ws.root / "data").mkdir()
    os.mkfifo(ws.root / "data" / "fifo")
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("push", "data/fifo")

    assert res.rc == 1
    assert "regular file, directory, or symlink" in res.err


def test_noop_subpath_data_only_push_does_not_run_post_hook(ws):
    marker = ws.root / "hook-ran"
    ws.write("data/sub/a.txt", "a")
    ws.config(
        {"data": {"path": str(ws.root / "data"), "post_hook": f"touch {shlex.quote(str(marker))}"}}
    )
    ws.run("push", "data", expect_rc=0)
    marker.unlink()

    ws.run("push", "--data-only", "data/sub", expect_rc=0)

    assert not marker.exists()


def test_local_path_resolution_handles_an_entry_at_filesystem_root(tmp_path):
    from s3bak import cli

    root = tmp_path.anchor
    cfg = cli.Config(
        profile="p",
        prefix="s3://bucket",
        bucket="bucket",
        path_prefix="",
        entries={"root": {"path": root}},
    )

    entry, sub = cli._resolve_one_arg(cfg, str(tmp_path / "child.txt"))

    assert entry == "root"
    assert sub == str(tmp_path / "child.txt").removeprefix(root).replace(os.sep, "/")
