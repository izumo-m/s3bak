"""push / pull / ls-remote round-trips against the live endpoint."""

from __future__ import annotations

import os
import shutil

from s3bak import cli


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


def test_push_with_only_a_local_delete_does_not_touch_the_manifest(ws):
    # Nothing to transfer and the kept record is not a structural change, so
    # the push is a full no-op (no manifest re-upload, no output).
    ws.write("data/a.txt", "a")
    ws.write("data/b.txt", "b")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "b.txt").unlink()
    res = ws.run("push", "data", expect_rc=0)

    assert res.out == ""
    assert "Updating" not in res.err


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


def test_push_meta_only_keeps_records_of_locally_deleted_files(ws):
    ws.write("data/a.txt", "a")
    ws.write("data/b.txt", "b")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "b.txt").unlink()
    ws.run("push", "--meta-only", "data", expect_rc=0)

    manifest_body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")[
        "Body"
    ].read()
    assert b'"path":"./b.txt"' in manifest_body


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
    # Deepest-first prompting: the nested file is asked first; keeping it makes
    # its ancestor extra dir unremovable, so the dir is kept without a prompt.
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.write("data/extradir/inner.txt", "i")
    ws.write("data/extra.txt", "e")

    answers.feed("n", "y")  # keep extradir/inner.txt, delete extra.txt
    ws.run("pull", "--delete", "data", expect_rc=0)

    assert len(answers.prompts) == 2
    assert "extradir" in answers.prompts[0] and "inner.txt" in answers.prompts[0]
    assert "extra.txt" in answers.prompts[1]
    assert (ws.root / "data" / "extradir" / "inner.txt").exists()
    assert not (ws.root / "data" / "extra.txt").exists()


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

    answers.feed("y", "q")  # extras prompt deepest-first: extra2 then extra1
    res = ws.run("pull", "--delete", "data")

    assert res.rc == 1
    assert "aborted" in res.err
    assert not (ws.root / "data" / "extra2.txt").exists()
    assert (ws.root / "data" / "extra1.txt").exists()


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
