"""push / pull / ls-remote round-trips against the live endpoint."""

from __future__ import annotations

import json
import os
import shutil

import pytest

from s3bak import cli


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


def test_single_file_push_data_only_ignores_mode_change(ws):
    local = ws.write("solo.txt", "content")
    os.chmod(local, 0o644)
    ws.config({"solo": {"path": str(local)}})
    ws.run("push", "solo", expect_rc=0)
    os.chmod(local, 0o600)

    res = ws.run("push", "--data-only", "solo", expect_rc=0)

    assert "Updating" not in res.err  # --data-only never touches the manifest
    assert '"mode":"100644"' in _manifest_body(ws, "solo")


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


def test_pull_single_file_reports_missing_object(ws):
    # A recorded single-file object deleted out-of-band: the pull must say
    # what is missing, not just exit 1.
    target = ws.write("solo.conf", "cfg")
    ws.config({"solo.conf": {"path": str(target)}})
    ws.run("push", "solo.conf", expect_rc=0)

    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/solo.conf")
    target.write_text("locally changed")  # defeat the all-matching short-circuit
    res = ws.run("pull", "solo.conf")

    assert res.rc == 1
    assert "object missing on S3" in res.err


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


# --- data-safety guarantees: staged root replacement, delete gating, re-settle ---


def test_pull_keeps_conflicting_dir_root_when_download_fails(ws):
    # A single-file entry restoring over a local DIRECTORY must not destroy it
    # before the download has succeeded: with the data object gone from S3,
    # the pull fails and the directory (and its contents) survive.
    ws.write("data", "payload")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data")

    dest = ws.root / "out"
    keep = ws.write("out/precious.txt", "keep me")

    res = ws.run("pull", "data", "-o", str(dest))
    assert res.rc == 1
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
        lambda self, *a, **k: TransferResult(returncode=1, stderr="injected failure"),
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
