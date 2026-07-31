"""Option coverage: --all, --meta-only, --data-only, --dry-run, --color."""

from __future__ import annotations

import os
import shutil
from types import SimpleNamespace

import pytest

from s3bak import store
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
    ws.write("data/a.txt", "original-content")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    original_body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt")["Body"].read()

    # Drift the already-pushed file's content (a real content+size change, not
    # just a new untracked path) and add a brand-new file.
    ws.write("data/a.txt", "edited-locally-and-never-pushed")
    ws.write("data/new.txt", "new")
    ws.run("push", "--meta-only", "data", expect_rc=0)

    assert "data/new.txt" not in ws.keys()  # the new file was not uploaded either
    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt")["Body"].read()
    assert body == original_body  # the existing object still holds the OLD bytes
    manifest_body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")[
        "Body"
    ].read()
    assert "new.txt" in manifest_body.decode()  # but it is recorded in the manifest

    # The refresh is a fresh local walk (docs/sync.md): it now records a.txt's
    # edited size/mtime too, so --meta-only "asserts S3 matches local without
    # making it true" - status goes clean even though the object is stale.
    res = ws.run("status", "data", expect_rc=0)
    assert res.out.strip() == ""


def test_meta_only_records_mode_change_and_clears_status(ws):
    f = ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    os.chmod(f, 0o600)
    res = ws.run("status", "data", expect_rc=0)
    assert "mode" in res.out  # the sync transfers nothing over a mode change

    ws.run("push", "--meta-only", "data", expect_rc=0)
    res = ws.run("status", "data", expect_rc=0)
    assert res.out.strip() == ""


def test_push_meta_only_dry_run_validates_the_manifest(ws):
    # A dry run performs the read-only work for real: the --meta-only refresh
    # downloads and validates the old manifest, so a damaged one fails the
    # rehearsal exactly like the real push.
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.s3.put_object(
        Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl", Body=b"not a manifest\n"
    )
    res = ws.run("push", "--meta-only", "--dry-run", "data")
    assert res.rc == 1


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


def test_push_data_only_warns_about_unrecorded_uploads(ws):
    # A --data-only upload of a file the manifest never recorded leaves an
    # unrecorded object (storage.md); the push must say so. cli.run maps the
    # warning to exit 2; in-process main() reports it on stderr only.
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "a.txt").write_text("a-changed")  # recorded: update lane
    ws.write("data/new.txt", "n")  # unrecorded: create lane

    res = ws.run("push", "--data-only", "data", expect_rc=0)
    assert "1 object(s) the manifest does not record" in res.err
    assert "./new.txt" not in _manifest_paths(ws)


def test_push_data_only_warns_again_for_an_object_it_left_unrecorded(ws):
    # The creating run counts the upload on the create lane; the object then
    # exists on S3, so the next --data-only run meets it as an update pair -
    # ManifestFilter re-uploads the manifest-unknown key and the warning must
    # repeat, not go silent after the first run (the cron case). A push
    # without --data-only then records it and ends the warnings.
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.write("data/new.txt", "n")

    for _ in range(2):
        res = ws.run("push", "--data-only", "data", expect_rc=0)
        assert "1 object(s) the manifest does not record" in res.err

    res = ws.run("push", "data", expect_rc=0)
    assert "does not record" not in res.err
    assert "./new.txt" in _manifest_paths(ws)


def test_push_data_only_of_recorded_files_does_not_warn(ws):
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "a.txt").write_text("a-changed")
    res = ws.run("push", "--data-only", "data", expect_rc=0)
    assert "does not record" not in res.err


def test_first_push_data_only_warns_for_every_upload(ws):
    # No manifest on S3 at all: every upload is unrecorded.
    ws.write("data/a.txt", "a")
    ws.write("data/b.txt", "b")
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("push", "--data-only", "data", expect_rc=0)
    assert "2 object(s) the manifest does not record" in res.err


def test_push_checksum_data_only_still_warns(ws):
    # --checksum --data-only used to skip the manifest download entirely; the
    # unrecorded-upload warning is why every dir push now fetches it.
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.write("data/new.txt", "n")
    res = ws.run("push", "--checksum", "--data-only", "data", expect_rc=0)
    assert "1 object(s) the manifest does not record" in res.err


def test_push_data_only_dry_run_previews_the_warning(ws):
    # The dry run makes the same lane decisions as the real push, so it
    # previews the warning ("would upload") while transferring nothing.
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.write("data/new.txt", "n")
    res = ws.run("push", "--data-only", "--dry-run", "data", expect_rc=0)
    assert "would upload 1 object(s) the manifest does not record" in res.err
    assert "new.txt" in res.out
    assert "data/new.txt" not in ws.keys()  # nothing was actually uploaded


def test_subpath_push_data_only_warns_about_unrecorded_uploads(ws):
    # The sub-relative compare key must be entry-rooted before the manifest
    # lookup, or a recorded sub file would be miscounted as unrecorded.
    ws.write("data/sub/x.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "sub" / "x.txt").write_text("x-changed")
    ws.write("data/sub/new.txt", "n")
    res = ws.run("push", "--data-only", "data/sub", expect_rc=0)
    assert "1 object(s) the manifest does not record" in res.err


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


def test_pull_meta_only_restores_mode_without_download(ws, monkeypatch):
    import botocore.client

    f = ws.write("data/a.txt", "recorded-content")
    ws.config({"data": {"path": str(ws.root / "data")}})
    os.chmod(f, 0o640)
    ws.run("push", "data", expect_rc=0)
    recorded_mtime_ns = os.stat(f).st_mtime_ns

    dest = ws.root / "restore"
    dest.mkdir()
    # Content, size, and mtime all disagree with the backup - the exact
    # condition under which a plain pull is certain to re-download (see
    # test_pull_without_meta_only_downloads_the_same_drift below). If
    # --meta-only ever ran the data lane, this local content would be
    # overwritten with "recorded-content".
    (dest / "a.txt").write_text("locally-drifted-content-of-a-different-length")
    os.utime(dest / "a.txt", (1_000_000_000, 1_000_000_000))
    os.chmod(dest / "a.txt", 0o600)  # the mode is wrong too

    calls: list[tuple[str, dict]] = []
    original_make_api_call = botocore.client.BaseClient._make_api_call

    def spy(self, operation_name, api_params):
        calls.append((operation_name, dict(api_params)))
        return original_make_api_call(self, operation_name, api_params)

    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", spy)
    ws.run("pull", "--meta-only", "data", "-o", str(dest), expect_rc=0)

    # Never downloaded: the local (wrong) content survives untouched.
    assert (dest / "a.txt").read_text() == "locally-drifted-content-of-a-different-length"
    # But the recorded metadata IS applied: mode and the file's own mtime.
    assert (os.stat(dest / "a.txt").st_mode & 0o777) == 0o640
    assert os.stat(dest / "a.txt").st_mtime_ns == recorded_mtime_ns

    # Direct observation of the data lane, not just its absence of effect:
    # the data object's key is never fetched. (The manifest key IS fetched
    # by every pull, --meta-only included - a distinct GetObject this does
    # not, and must not, assert away.)
    data_key = f"{ws.prefix}/data/a.txt"
    assert not any(
        op in ("GetObject", "HeadObject") and params.get("Key") == data_key for op, params in calls
    )


def test_pull_without_meta_only_downloads_the_same_drift(ws):
    # Contrast for the test above: the identical local/backup mismatch,
    # without --meta-only, is certainly re-downloaded - proving that scenario
    # is not something the size+mtime no-op gate would have skipped anyway.
    ws.write("data/a.txt", "recorded-content")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "restore"
    dest.mkdir()
    (dest / "a.txt").write_text("locally-drifted-content-of-a-different-length")
    os.utime(dest / "a.txt", (1_000_000_000, 1_000_000_000))

    ws.run("pull", "data", "-o", str(dest), expect_rc=0)

    assert (dest / "a.txt").read_text() == "recorded-content"


def test_pull_meta_only_repairs_dir_mode_and_symlink_target(ws):
    # --meta-only's gated apply covers more than a file's mode: a directory's
    # own mode/mtime and a symlink's target are also repaired - both
    # objectless records, so there was never data to download for them either
    # way, but this pins that --meta-only still reaches them.
    ws.write("data/sub/keep.txt", "keep")
    os.symlink("keep.txt", ws.root / "data" / "sub" / "link")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    sub = ws.root / "data" / "sub"
    link = ws.root / "data" / "sub" / "link"
    recorded_dir_mode = os.stat(sub).st_mode & 0o777

    os.chmod(sub, 0o700 if recorded_dir_mode != 0o700 else 0o750)
    os.remove(link)
    os.symlink("nope.txt", link)  # wrong target

    ws.run("pull", "--meta-only", "data", expect_rc=0)

    assert (os.stat(sub).st_mode & 0o777) == recorded_dir_mode
    assert os.readlink(link) == "keep.txt"


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
    assert "delete record:" in res.out  # ./sub's directory record, auto-confirmed
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
    # a is sticky across candidate kinds: ./sub's directory record - asked
    # post-order, once its children resolved deleted - drops without another
    # question.
    assert _manifest_paths(ws) == [".", "./keep.txt"]


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


def _objectless_orphans(ws) -> None:
    """Push a tree, then delete a file, a symlink, and an empty directory
    locally: the file's object is an ordinary delete candidate, and the two
    objectless records become record-only candidates (the record IS their
    backup). Candidate order is ascending by key: emptydir/, gone.txt, link."""
    ws.write("data/keep.txt", "k")
    ws.write("data/gone.txt", "g")
    (ws.root / "data" / "emptydir").mkdir()
    os.symlink("keep.txt", ws.root / "data" / "link")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    (ws.root / "data" / "gone.txt").unlink()
    (ws.root / "data" / "link").unlink()
    (ws.root / "data" / "emptydir").rmdir()


def test_push_delete_interactive_offers_objectless_records(ws, answers):
    # A vanished symlink or empty directory leaves a record with no object,
    # so --delete asks about the record itself: the symlink on arrival, the
    # directory at its pop (vacuously "everything beneath is gone" here).
    # Each prompt names the record kind; n keeps each record.
    _objectless_orphans(ws)

    answers.feed("n", "n", "n")
    res = ws.run("push", "--delete", "data", expect_rc=0)

    assert len(answers.prompts) == 3
    assert "data/emptydir/ (directory record)" in answers.prompts[0]
    assert "data/gone.txt" in answers.prompts[1]
    assert "data/link (symlink record)" in answers.prompts[2]
    assert "delete record:" not in res.out
    paths = _manifest_paths(ws)
    assert "./gone.txt" in paths
    assert "./link" in paths
    assert "./emptydir" in paths


def test_push_delete_interactive_drops_objectless_records_on_y(ws, answers):
    _objectless_orphans(ws)

    answers.feed("y", "y", "y")
    res = ws.run("push", "--delete", "data", expect_rc=0)

    assert res.out.count("delete record:") == 2  # emptydir/ and link
    assert _manifest_paths(ws) == [".", "./keep.txt"]
    # The retired records stay retired: nothing left to ask, nothing to D.
    second = ws.run("push", "--delete", "data", expect_rc=0)
    assert second.out == ""
    status = ws.run("status", "data", expect_rc=0)
    assert "D " not in status.out


def _nested_orphan_tree(ws) -> None:
    """Push a nested tree, then delete the whole `skills/` subtree locally.
    Candidates arrive in ascending key order; directory records resolve
    post-order, each once the stream has left its subtree."""
    ws.write("data/keep.txt", "k")
    ws.write("data/skills/top.txt", "t")
    ws.write("data/skills/evals/e1.txt", "e")
    ws.write("data/skills/evals/deep/d1.txt", "d")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    shutil.rmtree(ws.root / "data" / "skills")


def test_push_delete_offers_directory_records_post_order(ws, answers):
    # The order is subtree by subtree - children before their own directory,
    # in the same ascending key order as everything else (the push twin of
    # pull --delete's extras removal): d1.txt, deep/, e1.txt, evals/,
    # top.txt, skills/. All-y empties the backup of the vanished subtree.
    _nested_orphan_tree(ws)

    answers.feed("y", "y", "y", "y", "y", "y")
    ws.run("push", "--delete", "data", expect_rc=0)

    probes = [
        "skills/evals/deep/d1.txt",
        "data/skills/evals/deep/ (directory record)",
        "skills/evals/e1.txt",
        "data/skills/evals/ (directory record)",
        "skills/top.txt",
        "data/skills/ (directory record)",
    ]
    assert len(answers.prompts) == len(probes)
    for prompt, probe in zip(answers.prompts, probes, strict=True):
        assert probe in prompt
    assert _manifest_paths(ws) == [".", "./keep.txt"]
    assert ws.keys() == {"data/keep.txt", "data-manifest.jsonl"}
    status = ws.run("status", "data", expect_rc=0)
    assert "D " not in status.out
    # Converged: nothing left to offer, nothing to rewrite.
    second = ws.run("push", "--delete", "data", expect_rc=0)
    assert len(answers.prompts) == len(probes)
    assert second.out == ""
    assert "Updating" not in second.err


def test_push_delete_keeping_a_grandchild_keeps_every_open_ancestor_record(ws, answers):
    # n on the deepest file pins the whole ancestor chain: every directory
    # record still open above it is kept silently - no question of its own -
    # so the published manifest keeps the parent chain the validator demands.
    _nested_orphan_tree(ws)

    answers.feed("n", "y", "y")
    ws.run("push", "--delete", "data", expect_rc=0)

    assert len(answers.prompts) == 3  # d1.txt, e1.txt, top.txt - no dir prompts
    assert "d1.txt" in answers.prompts[0]
    assert "e1.txt" in answers.prompts[1]
    assert "top.txt" in answers.prompts[2]
    assert _manifest_paths(ws) == [
        ".",
        "./keep.txt",
        "./skills",
        "./skills/evals",
        "./skills/evals/deep",
        "./skills/evals/deep/d1.txt",
    ]
    assert "data/skills/evals/deep/d1.txt" in ws.keys()
    res = ws.run("verify", "data", expect_rc=0)
    assert "data: OK" in res.out


def test_push_delete_dry_run_lists_record_candidates(ws):
    # The dry run reports every candidate the completeness gate admits -
    # record-only candidates included, one "(dry-run) delete record:" line
    # each - and changes nothing: the rehearsal merge runs to a temp file.
    _nested_orphan_tree(ws)

    res = ws.run("push", "--delete", "--dry-run", "data", expect_rc=0)

    assert res.out.count("(dry-run) delete record:") == 3
    assert "(dry-run) delete record: " + f"s3://{ws.bucket}/{ws.prefix}/data/skills/" in res.out
    assert "(dry-run) would update manifest" in res.out
    keys = ws.keys()
    assert "data/skills/top.txt" in keys  # nothing deleted
    paths = _manifest_paths(ws)
    assert "./skills/evals/deep/d1.txt" in paths  # nothing dropped


def test_push_delete_all_no_run_skips_the_manifest_rewrite(ws):
    # A non-TTY --delete answers no to everything: the directory-record
    # candidates leave only no-change placeholder lines in the journal, and a
    # journal without one real event must not rewrite (or re-upload) the
    # manifest - a cron run would otherwise republish it forever.
    _nested_orphan_tree(ws)
    ws.run("push", "data", expect_rc=0)  # settle the root-mtime drift first

    res = ws.run("push", "--delete", "data", expect_rc=0)

    assert res.out == ""
    assert "Updating" not in res.err
    assert "./skills/evals/deep/d1.txt" in _manifest_paths(ws)


def test_push_delete_prunes_records_kept_under_a_file(ws, answers):
    # dir -> file replacement, records kept (the restorability warning case):
    # once --delete retires the shadowed objects, the loser directory record
    # is offered too, and the warning goes with it.
    ws.write("data/d/x.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    shutil.rmtree(ws.root / "data" / "d")
    (ws.root / "data" / "d").write_text("now a file")
    res = ws.run("push", "data", expect_rc=0)
    assert "non-directory" in res.err

    answers.feed("y", "y")
    res = ws.run("push", "--delete", "data", expect_rc=0)

    assert "non-directory" not in res.err
    assert "data/d/ (directory record)" in answers.prompts[1]
    assert _manifest_paths(ws) == [".", "./d"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo on this platform")
def test_push_delete_offers_special_file_record(ws, answers):
    ws.write("data/keep.txt", "k")
    os.mkfifo(ws.root / "data" / "fifo")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    (ws.root / "data" / "fifo").unlink()

    answers.feed("y")
    res = ws.run("push", "--delete", "data", expect_rc=0)

    assert len(answers.prompts) == 1
    assert "data/fifo (special-file record)" in answers.prompts[0]
    assert "delete record:" in res.out
    assert _manifest_paths(ws) == [".", "./keep.txt"]


def test_push_delete_dry_run_yes_lists_record_candidates(ws):
    # --yes and its rehearsal share one path: the real run prints the same
    # delete record: lines unmarked, the dry run marks them (dry-run) and
    # deletes nothing.
    _nested_orphan_tree(ws)

    res = ws.run("push", "--delete", "--yes", "--dry-run", "data", expect_rc=0)

    assert res.out.count("(dry-run) delete record:") == 3
    assert "data/skills/top.txt" in ws.keys()  # nothing deleted


def test_push_delete_failed_sync_asks_nothing_in_the_drain(ws, answers, monkeypatch):
    # A sync that stops mid-stream (an error, an interrupt) parks the
    # manifest cursor; the records it never reached are not evidence of
    # deletion. close()'s drain must keep them all without a question or a
    # "delete record:" line - the completeness gate, not a judgment call.
    _nested_orphan_tree(ws)

    def failed_sync(self, *args, **kwargs):
        return SimpleNamespace(returncode=1, results=0)

    monkeypatch.setattr(store.Boto3S3Store, "sync_up", failed_sync)
    res = ws.run("push", "--delete", "data", expect_rc=1)

    assert answers.prompts == []
    assert "delete record:" not in res.out
    assert "./skills/evals/deep/d1.txt" in _manifest_paths(ws)  # nothing dropped


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs an unreadable directory")
def test_push_delete_gate_counts_suppressed_record_candidates(ws):
    # Once the walk warns, a record-only candidate is kept without a
    # question - and must still count in the kept-candidates warning, which
    # would otherwise not fire at all on a record-only run.
    ws.write("data/keep.txt", "k")
    (ws.root / "data" / "aa").mkdir()  # sorts first: its warning precedes zlink's skip-over
    os.symlink("keep.txt", ws.root / "data" / "zlink")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "zlink").unlink()
    os.chmod(ws.root / "data" / "aa", 0)
    try:
        res = ws.run("push", "--delete", "--yes", "data")
    finally:
        os.chmod(ws.root / "data" / "aa", 0o755)

    assert res.rc == 0  # cli.main; cli.run maps the warnings to exit 2
    assert "kept 1 deletion candidate(s)" in res.err
    assert "./zlink" in _manifest_paths(ws)


def test_push_delete_yes_keeps_directory_record_pinned_by_a_shadowed_record(ws):
    # A key can hold a real S3 object AND a non-file record (a pushed file
    # later replaced by a symlink keeps its object until a --delete). When
    # the directory then vanishes locally, --yes deletes the object but must
    # keep the symlink record and its ancestor directory record - dropping
    # ./d would strand ./d/x.txt and fail the pre-publish validation after
    # the object was already gone. The next run retires the pair cleanly.
    ws.write("data/keep.txt", "k")
    ws.write("data/d/x.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    (ws.root / "data" / "d" / "x.txt").unlink()
    os.symlink("gone", ws.root / "data" / "d" / "x.txt")
    ws.run("push", "data", expect_rc=0)  # records the symlink; the object stays
    shutil.rmtree(ws.root / "data" / "d")

    ws.run("push", "--delete", "--yes", "data", expect_rc=0)

    assert "data/d/x.txt" not in ws.keys()  # the shadowed object went
    paths = _manifest_paths(ws)
    assert "./d" in paths
    assert "./d/x.txt" in paths  # the surviving record pinned its parent

    ws.run("push", "--delete", "--yes", "data", expect_rc=0)
    assert _manifest_paths(ws) == [".", "./keep.txt"]  # converged


def test_push_delete_directory_record_flip_crosses_the_write_buffer(ws, answers):
    # The placeholder flip seeks back to an offset that has long left the
    # journal's 8 KiB write buffer: ~120 dropped children put well over
    # 20 KiB between the directory's line and its flip. The published
    # manifest proves the one-byte overwrite landed on the marker.
    ws.write("data/keep.txt", "k")
    for i in range(120):
        ws.write(f"data/big/file-{i:04d}-{'x' * 60}.txt", "d")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    shutil.rmtree(ws.root / "data" / "big")

    answers.feed("a")
    ws.run("push", "--delete", "data", expect_rc=0)

    assert len(answers.prompts) == 1
    assert _manifest_paths(ws) == [".", "./keep.txt"]


def test_push_delete_with_all_answers_no_converges(ws):
    # A kept record must not read as "structure changed" beyond the directory
    # mtime the deletion itself bumped: the first non-TTY push --delete
    # settles that drift, and the second run - with nothing left to see -
    # may not rewrite the manifest or produce output, or a cron mirror would
    # re-upload and fire post_hook forever.
    ws.write("data/keep.txt", "k")
    ws.write("data/gone.txt", "g")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    (ws.root / "data" / "gone.txt").unlink()

    first = ws.run("push", "--delete", "data", expect_rc=0)
    second = ws.run("push", "--delete", "data", expect_rc=0)

    assert first.out == ""
    assert second.out == ""
    assert "Updating" in first.err  # the deletion bumped the directory's own mtime
    assert "Updating" not in second.err  # settled: converges


@pytest.mark.parametrize("delete_flag", [[], ["--delete"]])
def test_push_heals_stale_record_whose_object_is_gone(ws, delete_flag):
    # A record whose object vanished (an interrupted deletion, a q after a y,
    # ...) restores nothing, so retiring it is repair rather than deletion:
    # ANY push drops it - --delete or not - and without a question.
    ws.write("data/keep.txt", "k")
    ws.write("data/stale.txt", "s")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    (ws.root / "data" / "stale.txt").unlink()
    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/stale.txt")

    res = ws.run("push", *delete_flag, "data", expect_rc=0)

    assert "Updating" in res.err
    assert "./stale.txt" not in _manifest_paths(ws)

    dest = ws.root / "restore"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert (dest / "keep.txt").read_text() == "k"


def test_push_without_delete_keeps_the_record_of_a_backed_up_vanished_file(ws):
    # The counterpart of the heal above, and the reason the delete lane is
    # observed even without --delete: here the local file is gone but its
    # object is still on S3, so the backup stands. Seeing the object through
    # the lane is what tells this record from a stale one - skip it, and the
    # record would look objectless and be dropped, orphaning the object.
    ws.write("data/keep.txt", "k")
    ws.write("data/gone.txt", "g")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    (ws.root / "data" / "gone.txt").unlink()

    ws.run("push", "data", expect_rc=0)

    assert "data/gone.txt" in ws.keys()
    assert "./gone.txt" in _manifest_paths(ws)
    # Still offered - and still deletable - by a later --delete run.
    ws.run("push", "--delete", "--yes", "data", expect_rc=0)
    assert "data/gone.txt" not in ws.keys()
    assert "./gone.txt" not in _manifest_paths(ws)


def test_file_subpath_push_keeps_records_it_cannot_prove_stale(ws):
    # A single-file sub-path push lists no objects at all, so a record below
    # the sub path is not provably objectless: it must survive, object and
    # record together, until a directory-level push can see the S3 side.
    ws.write("data/a/inner.txt", "i")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    shutil.rmtree(ws.root / "data" / "a")
    ws.write("data/a", "now a file")

    ws.run("push", "data/a")

    assert "data/a/inner.txt" in ws.keys()
    assert "./a/inner.txt" in _manifest_paths(ws)


def test_push_delete_flags_candidates_missing_from_the_manifest(ws, answers):
    # An object the manifest never recorded (an out-of-band upload, or the
    # residue of an interrupted push) is offered like any orphan but flagged:
    # n keeps its object for this run only - nothing can be recorded for it,
    # so the manifest stays unchanged and a later --delete asks again.
    ws.write("data/keep.txt", "k")
    ws.write("data/gone.txt", "g")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    (ws.root / "data" / "gone.txt").unlink()
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/rogue.bin", Body=b"r")

    answers.feed("n", "n")
    ws.run("push", "--delete", "data", expect_rc=0)

    assert len(answers.prompts) == 2
    recorded = next(p for p in answers.prompts if "gone.txt" in p)
    rogue = next(p for p in answers.prompts if "rogue.bin" in p)
    assert "(not in manifest)" not in recorded
    assert "(not in manifest)" in rogue
    assert "data/rogue.bin" in ws.keys()
    assert "./rogue.bin" not in _manifest_paths(ws)


def test_subpath_push_delete_checksum_still_flags_unrecorded_candidates(ws, answers):
    # The sub-relative candidate key is joined back to the entry-rooted rel
    # for the manifest lookup. --checksum ignores the manifest for its compare,
    # but --delete still downloads it so the prompt can flag unrecorded
    # objects instead of flagging everything.
    ws.write("data/sub/x.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    (ws.root / "data" / "sub" / "x.txt").unlink()
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/sub/rogue.bin", Body=b"r")

    answers.feed("n", "n")
    ws.run("push", "--delete", "--checksum", "data/sub", expect_rc=0)

    assert len(answers.prompts) == 2
    x = next(p for p in answers.prompts if "x.txt" in p)
    rogue = next(p for p in answers.prompts if "rogue.bin" in p)
    assert "(not in manifest)" not in x
    assert "(not in manifest)" in rogue


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


def test_resolve_pull_destination_treats_native_sep_as_container():
    # A configured path ending in the native separator is a container: the entry
    # name is appended. On Windows os.sep is "\\", so checking only "/" would
    # miss it and restore to the container itself (a --delete data-loss risk).
    from s3bak import restore

    got = restore.resolve_pull_destination("data", f"/restore{os.sep}", None, None)
    assert got == os.path.join("/restore", "data")
    # -o is exact: no append, even with a trailing separator
    assert restore.resolve_pull_destination("data", None, None, f"/out{os.sep}") == f"/out{os.sep}"


def test_post_hook_stdin_is_detached_from_terminal(ws, monkeypatch):
    # Entries push concurrently and a --delete confirmation may be reading stdin
    # on another thread; a hook must not steal that answer. _run_hook detaches
    # the hook's stdin (subprocess.DEVNULL).
    import subprocess

    from s3bak import commands

    captured: dict = {}

    def spy(cmd, **kwargs):
        captured["stdin"] = kwargs.get("stdin")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(commands.subprocess, "run", spy)
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data"), "post_hook": ["true"]}})
    ws.run("push", "data", expect_rc=0)
    assert captured["stdin"] is subprocess.DEVNULL


def test_interactive_q_stops_later_upload_only_entries(ws, answers):
    # push --all --delete runs interactively and serially: a q at the first
    # (sorted) entry's deletion prompt aborts the whole command, so a later entry
    # that would only upload must not run (its new file is never pushed).
    ws.write("a_del/keep.txt", "k")
    ws.write("a_del/gone.txt", "g")
    ws.write("b_new/only.txt", "o")
    ws.config(
        {
            "a_del": {"path": str(ws.root / "a_del")},
            "b_new": {"path": str(ws.root / "b_new")},
        }
    )
    ws.run("push", "--all", expect_rc=0)

    (ws.root / "a_del" / "gone.txt").unlink()  # a_del now has a deletion to confirm
    ws.write("b_new/fresh.txt", "new")  # b_new would upload this
    answers.feed("q")
    res = ws.run("push", "--all", "--delete")

    assert "aborted" in res.err
    assert "b_new/only.txt" in ws.keys()  # from the initial push
    assert "b_new/fresh.txt" not in ws.keys()  # the abort stopped the later entry
