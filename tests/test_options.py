"""Option coverage: --all, --meta-only, --data-only, --dry-run, --color."""

from __future__ import annotations

import os
import shutil

from s3bak.cli import _resolve_use_color


def test_push_all_uploads_every_entry(ws):
    ws.write("d1/a.txt", "a")
    ws.write("d2/b.txt", "b")
    ws.config({"d1": {"path": str(ws.root / "d1")}, "d2": {"path": str(ws.root / "d2")}})

    ws.run("push", "--all", expect_rc=0)

    keys = ws.keys()
    assert {"d1/a.txt", "d2/b.txt", "d1-manifest.jsonl", "d2-manifest.jsonl"} <= keys


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
    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")["Body"].read()
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
    before = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")[
        "Body"
    ].read()

    (ws.root / "data" / "a.txt").write_text("a-much-bigger-content")
    ws.run("push", "--data-only", "data", expect_rc=0)

    obj = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt")["Body"].read()
    assert obj == b"a-much-bigger-content"  # data was uploaded
    after = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")[
        "Body"
    ].read()
    assert before == after  # but the manifest was not rewritten


def test_push_dryrun_uploads_nothing(ws):
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("push", "--dry-run", "data", expect_rc=0)
    assert ws.keys() == set()  # nothing was actually uploaded
    assert "a.txt" in res.out  # the planned upload is reported


def test_pull_dryrun_changes_nothing(ws):
    ws.write("data/a.txt", "one")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "a.txt").write_text("drifted")
    res = ws.run("pull", "--dry-run", "data", expect_rc=0)

    assert (ws.root / "data" / "a.txt").read_text() == "drifted"  # not overwritten
    assert "(dry-run) download:" in res.out
    assert "would apply manifest metadata" in res.out


def test_pull_dryrun_clean_tree_prints_nothing(ws):
    ws.write("data/a.txt", "one")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("pull", "--dry-run", "data", expect_rc=0)
    assert res.out == ""


def test_pull_delete_dryrun_keeps_extras(ws):
    ws.write("data/a.txt", "one")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    extra = ws.write("data/extra.txt", "keep me")
    res = ws.run("pull", "--delete", "--dry-run", "data", expect_rc=0)

    assert extra.exists()  # the extra was reported, not removed
    assert "(dry-run) delete:" in res.out
    assert "extra.txt" in res.out


def test_pull_dryrun_missing_destination_creates_nothing(ws):
    # S3.sync creates a missing local destination even on a dry run (aws-cli
    # parity); pull --dry-run must clean that up to keep its no-changes promise.
    ws.write("data/sub/a.txt", "one")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    shutil.rmtree(ws.root / "data")
    res = ws.run("pull", "--dry-run", "data", expect_rc=0)

    assert not (ws.root / "data").exists()  # no directories left behind
    assert "(dry-run) download:" in res.out


def test_pull_dryrun_conflicting_root_reports_replacement(ws):
    # A restore root of the wrong type is replaced by a real pull; a dry run
    # must report the conflict and leave it alone.
    ws.write("data/a.txt", "one")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    shutil.rmtree(ws.root / "data")
    ws.write("data", "now a file")
    res = ws.run("pull", "--dry-run", "data", expect_rc=0)

    assert (ws.root / "data").read_text() == "now a file"  # untouched
    assert "would replace" in res.out


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


def test_pull_restores_multiple_explicit_entries(ws):
    ws.write("d1/a.txt", "a")
    ws.write("d2/b.txt", "b")
    ws.config({"d1": {"path": str(ws.root / "d1")}, "d2": {"path": str(ws.root / "d2")}})
    ws.run("push", "--all", expect_rc=0)

    (ws.root / "d1" / "a.txt").unlink()
    (ws.root / "d2" / "b.txt").unlink()
    ws.run("pull", "d1", "d2", expect_rc=0)

    assert (ws.root / "d1" / "a.txt").read_text() == "a"
    assert (ws.root / "d2" / "b.txt").read_text() == "b"


def test_pull_restores_multiple_explicit_subpaths(ws):
    ws.write("d1/a.txt", "a")
    ws.write("d2/b.txt", "b")
    ws.config({"d1": {"path": str(ws.root / "d1")}, "d2": {"path": str(ws.root / "d2")}})
    ws.run("push", "--all", expect_rc=0)

    (ws.root / "d1" / "a.txt").unlink()
    (ws.root / "d2" / "b.txt").unlink()
    ws.run("pull", "d1/a.txt", "d2/b.txt", expect_rc=0)

    assert (ws.root / "d1" / "a.txt").read_text() == "a"
    assert (ws.root / "d2" / "b.txt").read_text() == "b"


def test_pull_allows_disjoint_destinations_from_trailing_slash(ws):
    restore_root = ws.root / "restore"
    ws.write("restore/source-a.txt", "a")
    ws.write("restore/b/source-b.txt", "b")
    ws.config(
        {
            "a": {"path": f"{restore_root}/"},
            "b": {"path": str(restore_root / "b")},
        }
    )
    ws.run("push", "--all", expect_rc=0)
    shutil.rmtree(restore_root)

    ws.run("pull", "a", "b", expect_rc=0)

    assert (restore_root / "a" / "source-a.txt").read_text() == "a"
    assert (restore_root / "b" / "source-b.txt").read_text() == "b"


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

    ws.run("push", "--dry-run", "solo.txt", expect_rc=0)
    assert ws.keys() == set()


def test_pull_single_file_dryrun_downloads_nothing(ws):
    f = ws.write("solo.txt", "original")
    ws.config({"solo.txt": {"path": str(f)}})
    ws.run("push", "solo.txt", expect_rc=0)

    f.write_text("drifted")
    res = ws.run("pull", "--dry-run", "solo.txt", expect_rc=0)

    assert f.read_text() == "drifted"  # not overwritten
    assert "(dry-run) download:" in res.out
    assert "would apply manifest metadata" in res.out


def test_push_single_file_dryrun_prints_upload_once(ws):
    # Regression: the single-file dryrun path printed the upload line twice -
    # once directly and once via the shared results writer.
    f = ws.write("solo.txt", "x")
    ws.config({"solo.txt": {"path": str(f)}})

    res = ws.run("push", "--dry-run", "solo.txt", expect_rc=0)
    uploads = [ln for ln in res.out.splitlines() if ln.startswith("(dry-run) upload:")]
    assert len(uploads) == 1


def test_push_git_entry_meta_only_writes_manifest_like_any_other_entry(ws):
    ws.write("repo.git/HEAD", "ref")
    ws.config({"repo.git": {"path": str(ws.root / "repo.git")}})

    ws.run("push", "--meta-only", "repo.git", expect_rc=0)
    assert "repo.git-manifest.jsonl" in ws.keys()


# --- push --delete (confirmed deletions) ---------------------------------------


def _manifest_paths(ws) -> list[str]:
    import json

    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")["Body"].read()
    return [json.loads(ln)["path"] for ln in body.decode().splitlines()[1:]]


def _orphan_tree(ws) -> None:
    """Push a tree, then delete `sub/` locally: sub/x.txt and sub/y.txt become
    S3 orphans (delete candidates in that key order) while keep.txt stays."""
    ws.write("data/keep.txt", "k")
    ws.write("data/sub/x.txt", "x")
    ws.write("data/sub/y.txt", "y")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    shutil.rmtree(ws.root / "data" / "sub")


def test_push_delete_yes_mirrors_unattended(ws, answers):
    _orphan_tree(ws)

    res = ws.run("push", "--delete", "--yes", "data", expect_rc=0)

    assert answers.prompts == []
    assert "delete:" in res.out
    keys = ws.keys()
    assert "data/sub/x.txt" not in keys
    assert "data/sub/y.txt" not in keys
    assert "data/keep.txt" in keys
    assert _manifest_paths(ws) == [".", "./keep.txt"]


def test_push_delete_without_tty_answers_no_to_everything(ws):
    # pytest's stdin is not a TTY: --delete without --yes keeps everything,
    # succeeds (rc 0), and neither warns nor prompts.
    _orphan_tree(ws)

    res = ws.run("push", "--delete", "data", expect_rc=0)

    assert "delete:" not in res.out
    assert "warning" not in res.err.lower()
    keys = ws.keys()
    assert "data/sub/x.txt" in keys
    assert "data/sub/y.txt" in keys
    assert "./sub/x.txt" in _manifest_paths(ws)
    assert "./sub/y.txt" in _manifest_paths(ws)


def test_push_delete_interactive_y_n_mix_keeps_answered_records(ws, answers):
    # Candidates arrive in key order: sub/x.txt then sub/y.txt. Deleting x and
    # keeping y must keep y's record AND its ancestor dir record ./sub.
    _orphan_tree(ws)
    answers.feed("y", "n")

    res = ws.run("push", "--delete", "data", expect_rc=0)

    assert len(answers.prompts) == 2
    assert "sub/x.txt" in answers.prompts[0]
    keys = ws.keys()
    assert "data/sub/x.txt" not in keys
    assert "data/sub/y.txt" in keys
    assert _manifest_paths(ws) == [".", "./keep.txt", "./sub", "./sub/y.txt"]
    assert "delete:" in res.out


def test_push_delete_interactive_a_deletes_the_rest(ws, answers):
    _orphan_tree(ws)
    answers.feed("a")

    ws.run("push", "--delete", "data", expect_rc=0)

    assert len(answers.prompts) == 1
    assert ws.keys() == {"data/keep.txt", "data-manifest.jsonl"}
    # ./sub survives: a directory record has no object, so no confirmation can
    # drop it - only the --yes mirror prunes objectless records.
    assert _manifest_paths(ws) == [".", "./keep.txt", "./sub"]


def test_push_delete_interactive_d_keeps_the_rest(ws, answers):
    _orphan_tree(ws)
    answers.feed("d")

    ws.run("push", "--delete", "data", expect_rc=0)

    assert len(answers.prompts) == 1
    keys = ws.keys()
    assert "data/sub/x.txt" in keys
    assert "data/sub/y.txt" in keys
    assert _manifest_paths(ws) == [".", "./keep.txt", "./sub", "./sub/x.txt", "./sub/y.txt"]


def test_push_delete_interactive_q_aborts_without_manifest_update(ws, answers):
    hook_sentinel = ws.root / "hook-ran"
    ws.write("data/keep.txt", "k")
    ws.write("data/sub/x.txt", "x")
    ws.write("data/sub/y.txt", "y")
    ws.config(
        {
            "data": {
                "path": str(ws.root / "data"),
                "post_hook": ["python3", "-c", f"open({str(hook_sentinel)!r}, 'w').close()"],
            }
        }
    )
    ws.run("push", "data", expect_rc=0)
    assert hook_sentinel.exists()
    hook_sentinel.unlink()
    shutil.rmtree(ws.root / "data" / "sub")
    before = _manifest_paths(ws)

    answers.feed("q")
    res = ws.run("push", "--delete", "data")

    assert res.rc == 1
    assert "aborted" in res.err
    assert _manifest_paths(ws) == before
    assert not hook_sentinel.exists()
    assert "data/sub/y.txt" in ws.keys()  # never asked, never deleted


def test_push_delete_dry_run_reports_all_candidates_without_prompting(ws, answers):
    _orphan_tree(ws)
    before = _manifest_paths(ws)

    res = ws.run("push", "--delete", "--dry-run", "data", expect_rc=0)

    assert answers.prompts == []
    assert "(dry-run) delete:" in res.out
    assert "sub/x.txt" in res.out
    assert "sub/y.txt" in res.out
    assert "(dry-run) would update manifest" in res.out
    keys = ws.keys()
    assert "data/sub/x.txt" in keys
    assert "data/sub/y.txt" in keys
    assert _manifest_paths(ws) == before


def test_push_dry_run_without_delete_prints_no_delete_lines(ws):
    _orphan_tree(ws)

    res = ws.run("push", "--dry-run", "data", expect_rc=0)

    assert "delete:" not in res.out


def test_push_delete_on_single_file_entry_deletes_nothing(ws, answers):
    target = ws.write("single.txt", "x")
    ws.config({"single": {"path": str(target)}})
    ws.run("push", "single", expect_rc=0)

    ws.run("push", "--delete", "--yes", "single", expect_rc=0)

    assert "single" in ws.keys()
    assert answers.prompts == []


def test_push_all_delete_yes_mirrors_every_entry(ws):
    ws.write("d1/a.txt", "a")
    ws.write("d1/gone.txt", "g")
    ws.write("d2/b.txt", "b")
    ws.write("d2/gone.txt", "g")
    ws.config(
        {
            "d1": {"path": str(ws.root / "d1")},
            "d2": {"path": str(ws.root / "d2")},
        }
    )
    ws.run("push", "--all", expect_rc=0)
    (ws.root / "d1" / "gone.txt").unlink()
    (ws.root / "d2" / "gone.txt").unlink()

    ws.run("push", "--all", "--delete", "--yes", expect_rc=0)

    keys = ws.keys()
    assert "d1/gone.txt" not in keys
    assert "d2/gone.txt" not in keys
    assert "d1/a.txt" in keys
    assert "d2/b.txt" in keys


def test_push_delete_interactive_never_drops_objectless_records(ws, answers):
    # Symlinks and empty dirs have no S3 object, hence no delete question:
    # their records must survive an interactive --delete whatever the answers.
    ws.write("data/keep.txt", "k")
    ws.write("data/gone.txt", "g")
    (ws.root / "data" / "emptydir").mkdir()
    os.symlink("keep.txt", ws.root / "data" / "link")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "gone.txt").unlink()
    (ws.root / "data" / "link").unlink()
    (ws.root / "data" / "emptydir").rmdir()
    answers.feed("n")  # the only question: gone.txt's object
    ws.run("push", "--delete", "data", expect_rc=0)

    assert len(answers.prompts) == 1
    paths = _manifest_paths(ws)
    assert "./gone.txt" in paths
    assert "./link" in paths
    assert "./emptydir" in paths


def test_push_delete_with_all_answers_no_converges(ws):
    # A kept record must not read as "structure changed": the same non-TTY
    # push --delete run twice may not rewrite the manifest or produce output,
    # or a cron mirror would re-upload and fire post_hook forever.
    ws.write("data/keep.txt", "k")
    ws.write("data/gone.txt", "g")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    (ws.root / "data" / "gone.txt").unlink()

    first = ws.run("push", "--delete", "data", expect_rc=0)
    second = ws.run("push", "--delete", "data", expect_rc=0)

    for res in (first, second):
        assert res.out == ""
        assert "Updating" not in res.err


def test_push_delete_heals_stale_record_whose_object_is_gone(ws):
    # A record whose object vanished (interrupted deletion, q after y, ...)
    # is not a delete candidate, so no answer covers it: any --delete push -
    # including the unattended all-no run - drops it from the manifest.
    ws.write("data/keep.txt", "k")
    ws.write("data/stale.txt", "s")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    (ws.root / "data" / "stale.txt").unlink()
    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/stale.txt")

    res = ws.run("push", "--delete", "data", expect_rc=0)

    assert "Updating" in res.err
    assert "./stale.txt" not in _manifest_paths(ws)

    dest = ws.root / "restore"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert (dest / "keep.txt").read_text() == "k"


def test_file_subpath_push_delete_keeps_former_directory_records(ws, answers):
    # A file-typed sub-path has no S3 listing, so --delete has nothing to
    # confirm there: records under the same-named former directory survive
    # (with the restorability warning) whether or not a TTY is attached.
    ws.write("data/sub/x.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    shutil.rmtree(ws.root / "data" / "sub")
    (ws.root / "data" / "sub").write_text("now a file")
    res = ws.run("push", "--delete", "data/sub", expect_rc=0)

    assert answers.prompts == []
    assert "non-directory" in res.err
    paths = _manifest_paths(ws)
    assert "./sub/x.txt" in paths
    assert "data/sub/x.txt" in ws.keys()
