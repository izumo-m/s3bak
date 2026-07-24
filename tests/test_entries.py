"""Entry kinds: single-file entries, excludes, and `list`."""

from __future__ import annotations

import os
import shutil
import sys

import pytest


def _marker_hook(ws, marker) -> list[str]:
    hook = ws.write(
        "write-marker.py",
        "from pathlib import Path\nimport sys\nPath(sys.argv[1]).touch()\n",
    )
    return [sys.executable, str(hook), str(marker)]


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


def _manifest_paths(ws) -> list[str]:
    import json

    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")["Body"].read()
    return [json.loads(ln)["path"] for ln in body.decode().splitlines()[1:]]


def _push_then_exclude_cache(ws) -> None:
    """Push a tree, then add an exclude covering an already-pushed subtree.

    cache/ stays on disk: excludes prune only the sync's local side, so its
    S3 objects (and nothing else) become delete-lane orphans."""
    ws.write("data/keep.txt", "k")
    ws.write("data/cache/c.txt", "c")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    assert "data/cache/c.txt" in ws.keys()
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/*"]}})


def test_push_delete_offers_excluded_objects_as_orphans(ws, answers):
    # An exclude added after a push must not strand the pushed objects: the
    # local side no longer lists cache/, so its S3 objects surface as ordinary
    # delete candidates - recorded ones, so no "(not in manifest)" flag - and
    # the confirmed deletion drops object and record together.
    _push_then_exclude_cache(ws)

    answers.feed("y")
    res = ws.run("push", "--delete", "data", expect_rc=0)

    assert len(answers.prompts) == 1
    assert "cache/c.txt" in answers.prompts[0]
    assert "(not in manifest)" not in answers.prompts[0]
    assert "delete:" in res.out
    assert "data/cache/c.txt" not in ws.keys()
    assert "./cache/c.txt" not in _manifest_paths(ws)
    assert (ws.root / "data" / "cache" / "c.txt").read_text() == "c"  # local untouched


def test_push_delete_yes_prunes_excluded_objects_unattended(ws, answers):
    _push_then_exclude_cache(ws)

    ws.run("push", "--delete", "--yes", "data", expect_rc=0)

    assert answers.prompts == []
    assert "data/cache/c.txt" not in ws.keys()
    assert _manifest_paths(ws) == [".", "./keep.txt"]


def test_push_delete_answer_n_keeps_excluded_object_and_record(ws, answers):
    # n keeps the pair - even through a manifest rewrite forced by another
    # change, the kept record must survive the journal merge (n journals no
    # drop), so the entry still verifies clean and a later --delete asks
    # again.
    _push_then_exclude_cache(ws)
    (ws.root / "data" / "keep.txt").write_text("k-v2")  # forces a rewrite

    answers.feed("n")
    ws.run("push", "--delete", "data", expect_rc=0)

    assert len(answers.prompts) == 1
    assert "data/cache/c.txt" in ws.keys()
    assert "./cache/c.txt" in _manifest_paths(ws)
    res = ws.run("verify", "data", expect_rc=0)
    assert "data: OK" in res.out


def test_push_delete_dry_run_lists_excluded_orphans_without_prompting(ws, answers):
    _push_then_exclude_cache(ws)

    res = ws.run("push", "--delete", "--dry-run", "data", expect_rc=0)

    assert answers.prompts == []
    assert "(dry-run)" in res.out and "delete:" in res.out
    assert "cache/c.txt" in res.out
    assert "data/cache/c.txt" in ws.keys()
    assert "./cache/c.txt" in _manifest_paths(ws)


def test_plain_push_neither_uploads_nor_deletes_excluded_paths(ws):
    # Without --delete the exclude only stops uploads: the stranded object
    # stays (deleting is never the default) and verify keeps reporting the
    # entry as intact - record and object still correspond.
    _push_then_exclude_cache(ws)

    (ws.root / "data" / "cache" / "c.txt").write_text("changed")
    res = ws.run("push", "data", expect_rc=0)

    assert "upload:" not in res.out
    assert "delete:" not in res.out
    assert "data/cache/c.txt" in ws.keys()
    res = ws.run("verify", "data", expect_rc=0)
    assert "data: OK" in res.out


def test_push_delete_retires_unrecorded_excluded_object(ws, answers):
    # The verify deadlock this design fixes: an object under an excluded path
    # that the manifest never recorded (out-of-band upload, or residue of the
    # old both-sides exclude semantics). verify warns about it, and push
    # --delete - not a manual `aws s3 rm` - must be able to retire it.
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["logs.db"]}})
    ws.run("push", "data", expect_rc=0)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/logs.db", Body=b"stray")

    res = ws.run("verify", "data", expect_rc=0)
    assert "unrecorded object" in res.err and "logs.db" in res.err

    answers.feed("y")
    ws.run("push", "--delete", "data", expect_rc=0)

    assert len(answers.prompts) == 1
    assert "(not in manifest)" in answers.prompts[0]
    assert "data/logs.db" not in ws.keys()
    res = ws.run("verify", "data", expect_rc=0)
    assert "data: OK" in res.out


def test_subpath_push_delete_offers_excluded_objects(ws, answers):
    # The delete lane honors the entry-rooted excludes on a sub-path push too:
    # the sub walk re-roots at ./sub/, so "sub/cache/*" prunes the local side
    # and the pushed cache object becomes this sync's delete candidate.
    ws.write("data/sub/a.txt", "a")
    ws.write("data/sub/cache/c.txt", "c")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["sub/cache/*"]}})

    answers.feed("y")
    ws.run("push", "--delete", "data/sub", expect_rc=0)

    assert len(answers.prompts) == 1
    assert "sub/cache/c.txt" in answers.prompts[0]
    keys = ws.keys()
    assert "data/sub/cache/c.txt" not in keys
    assert "data/sub/a.txt" in keys
    assert "./sub/cache/c.txt" not in _manifest_paths(ws)
    assert "./sub/a.txt" in _manifest_paths(ws)


def test_subpath_push_of_excluded_subtree_backs_it_up(ws):
    # Explicitly pushing an excluded sub-path wins over the exclude, exactly
    # as the manifest walk (iter_subtree) already treats it: the sub's own
    # subtree is walked, uploaded, AND recorded, so data and manifest agree.
    # (The old both-sides filter uploaded nothing while the manifest patch
    # recorded everything - records whose objects never existed.)
    ws.write("data/keep.txt", "k")
    ws.write("data/tmp/x.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["tmp/*"]}})
    ws.run("push", "data", expect_rc=0)
    assert "data/tmp/x.txt" not in ws.keys()

    ws.run("push", "data/tmp", expect_rc=0)

    assert "data/tmp/x.txt" in ws.keys()
    assert "./tmp/x.txt" in _manifest_paths(ws)
    res = ws.run("verify", "data", expect_rc=0)
    assert "data: OK" in res.out


def test_entry_dir_replaced_by_file_refuses_without_delete(ws):
    # The entry path was a directory; it becomes a single file. An ordinary
    # push must refuse - recording the new kind would silently orphan the old
    # tree's objects - and leave the backup untouched.
    import shutil

    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    shutil.rmtree(ws.root / "data")
    (ws.root / "data").write_text("bye")

    res = ws.run("push", "data", expect_rc=1)
    assert "push --delete replaces the old backup" in res.err
    assert "data/a.txt" in ws.keys()  # the refused push changed nothing


def test_entry_dir_replaced_by_file_migrates_with_delete(ws):
    # push --delete --yes migrates the entry kind: the old tree's objects are
    # deleted and the single-file backup (object + file-shaped manifest)
    # replaces them, so every later command sees a consistent backup.
    import shutil

    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    shutil.rmtree(ws.root / "data")
    (ws.root / "data").write_text("bye")

    res = ws.run("push", "--delete", "--yes", "data", expect_rc=0)
    assert "delete:" in res.out and "upload:" in res.out
    assert "data/a.txt" not in ws.keys()
    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data")["Body"].read()
    assert body == b"bye"
    ws.run("verify", "data", expect_rc=0)


def test_entry_file_replaced_by_dir_migrates_with_delete(ws):
    # The reverse kind change: a single-file entry becomes a directory. The
    # ordinary push refuses; --delete --yes deletes the old exact object and
    # records the directory from scratch (a bare-basename record must never
    # survive into a directory manifest).
    ws.write("data", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    os.remove(ws.root / "data")
    ws.write("data/b.txt", "b")

    ws.run("push", "data", expect_rc=1)
    ws.run("push", "--delete", "--yes", "data", expect_rc=0)
    assert "data" not in ws.keys()
    assert "data/b.txt" in ws.keys()
    ws.run("verify", "data", expect_rc=0)
    dest = ws.root / "out"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert (dest / "b.txt").read_text() == "b"


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


def test_dir_subpath_push_keeps_backup_by_default(ws):
    # A sub-path push is a whole-entry push scoped to the sub-path, and like a
    # whole-entry push it never deletes by default: the removed file's object
    # AND its manifest record both survive the patch.
    ws.write("data/keep.txt", "k")
    ws.write("data/sub/x.txt", "x")
    ws.write("data/sub/y.txt", "y")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "sub" / "y.txt").unlink()
    res = ws.run("push", "data/sub", expect_rc=0)

    assert "delete:" not in res.out
    keys = ws.keys()
    assert "data/sub/y.txt" in keys
    assert "data/sub/x.txt" in keys
    manifest_body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")[
        "Body"
    ].read()
    assert b'"path":"./sub/y.txt"' in manifest_body


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
    ws.config(
        {
            "data": {
                "path": str(ws.root / "data"),
                "post_hook": [sys.executable, "-c", "raise SystemExit(3)"],
            }
        }
    )
    res = ws.run("push", "data")
    assert res.rc == 3


def test_post_hook_exit_2_normalizes_to_hard_error(ws):
    # Exit 2 is reserved for a warnings-only s3bak run; a hook exiting 2 is a
    # hook failure and must not masquerade as one.
    ws.write("data/a.txt", "x")
    ws.config(
        {
            "data": {
                "path": str(ws.root / "data"),
                "post_hook": [sys.executable, "-c", "raise SystemExit(2)"],
            }
        }
    )
    res = ws.run("push", "data")
    assert res.rc == 1
    assert "post_hook failed (exit 2)" in res.err


def test_post_hook_signal_death_maps_to_128_plus_signal(ws):
    # subprocess reports a signal death as a negative returncode; sys.exit
    # must see the conventional 128+N instead.
    ws.write("data/a.txt", "x")
    ws.config(
        {
            "data": {
                "path": str(ws.root / "data"),
                "post_hook": [
                    sys.executable,
                    "-c",
                    "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
                ],
            }
        }
    )
    res = ws.run("push", "data")
    assert res.rc == 128 + 15


def test_post_hook_runs_on_success(ws):
    marker = ws.root / "hook-ran"
    ws.write("data/a.txt", "x")
    ws.config(
        {
            "data": {
                "path": str(ws.root / "data"),
                "post_hook": _marker_hook(ws, marker),
            }
        }
    )
    ws.run("push", "data", expect_rc=0)
    assert marker.exists()


def test_hook_arguments_are_passed_without_shell_expansion(ws):
    recorded = ws.root / "hook-argument"
    hook = ws.write(
        "record-argument.py",
        "from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text(sys.argv[2])\n",
    )
    literal = "~/backup/*.txt; exit 9"
    ws.write("data/a.txt", "x")
    ws.config(
        {
            "data": {
                "path": str(ws.root / "data"),
                "post_hook": [sys.executable, str(hook), str(recorded), literal],
            }
        }
    )

    ws.run("push", "data", expect_rc=0)

    assert recorded.read_text() == literal


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

    ws.run("push", "--delete", "--yes", "data/sub", expect_rc=0)

    keys = ws.keys()
    assert not any(key == "data/sub" or key.startswith("data/sub/") for key in keys)
    assert "data/sub.txt" in keys
    assert "data/submarine/b.txt" in keys
    manifest_body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")[
        "Body"
    ].read()
    assert b'"path":"./sub"' not in manifest_body


def test_missing_subpath_push_delete_without_tty_deletes_nothing(ws):
    ws.write("data/sub/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    shutil.rmtree(ws.root / "data" / "sub")

    res = ws.run("push", "--delete", "data/sub")

    assert res.rc == 1
    assert "--yes" in res.err
    assert "data/sub/a.txt" in ws.keys()
    manifest_body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")[
        "Body"
    ].read()
    assert b'"path":"./sub/a.txt"' in manifest_body


def test_missing_subpath_push_delete_asks_one_subtree_question(ws, answers):
    ws.write("data/sub/a.txt", "a")
    ws.write("data/sub/b.txt", "b")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    shutil.rmtree(ws.root / "data" / "sub")

    answers.feed("n")
    res = ws.run("push", "--delete", "data/sub")
    assert res.rc == 1
    assert "data/sub/a.txt" in ws.keys()

    answers.feed("y")
    ws.run("push", "--delete", "data/sub", expect_rc=0)
    assert len(answers.prompts) == 2  # one subtree question per run, not per key
    assert not any(key.startswith("data/sub/") for key in ws.keys())


def test_dir_subpath_push_delete_yes_mirrors_only_inside_the_sub(ws):
    ws.write("data/keep.txt", "k")
    ws.write("data/sub.txt", "sibling")
    ws.write("data/sub/x.txt", "x")
    ws.write("data/sub/y.txt", "y")
    ws.write("data/submarine/z.txt", "z")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "sub" / "y.txt").unlink()
    res = ws.run("push", "--delete", "--yes", "data/sub", expect_rc=0)

    assert "delete:" in res.out
    keys = ws.keys()
    assert "data/sub/y.txt" not in keys
    assert "data/sub/x.txt" in keys
    assert "data/sub.txt" in keys
    assert "data/submarine/z.txt" in keys
    res = ws.run("status", "data", expect_rc=0)
    assert res.out.strip() == ""


def test_dir_subpath_push_delete_interactive_keeps_answered_records(ws, answers):
    # Kept records inside the replaced range survive with their ancestor dir;
    # records outside the sub are copied verbatim as always.
    ws.write("data/keep.txt", "k")
    ws.write("data/sub/inner/x.txt", "x")
    ws.write("data/sub/inner/y.txt", "y")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    shutil.rmtree(ws.root / "data" / "sub" / "inner")
    answers.feed("y", "n")  # delete inner/x.txt, keep inner/y.txt
    ws.run("push", "--delete", "data/sub", expect_rc=0)

    keys = ws.keys()
    assert "data/sub/inner/x.txt" not in keys
    assert "data/sub/inner/y.txt" in keys
    manifest_body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")[
        "Body"
    ].read()
    assert b'"path":"./sub/inner/y.txt"' in manifest_body
    assert b'"path":"./sub/inner/x.txt"' not in manifest_body
    assert b'"path":"./sub/inner"' in manifest_body  # the kept file's parent dir
    assert b'"path":"./keep.txt"' in manifest_body


def test_subpath_push_is_rejected_for_single_file_entry(ws):
    target = ws.write("single.txt", "x")
    ws.config({"single": {"path": str(target)}})

    res = ws.run("push", "--delete", "single/child")

    assert res.rc == 1
    assert "single-file entry" in res.err


def test_pre_hook_can_create_entry_target(ws):
    target = ws.root / "generated.txt"
    hook = ws.write(
        "generate.py",
        "from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text('generated')\n",
    )
    ws.config(
        {
            "generated": {
                "path": str(target),
                "pre_hook": [sys.executable, str(hook), str(target)],
            }
        }
    )

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
        {
            "data": {
                "path": str(ws.root / "data"),
                "post_hook": _marker_hook(ws, marker),
            }
        }
    )
    ws.run("push", "data", expect_rc=0)
    marker.unlink()

    ws.run("push", "--data-only", "data/sub", expect_rc=0)

    assert not marker.exists()


def test_hook_string_is_rejected(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data"), "pre_hook": "do-something"}})

    res = ws.run("list")

    assert res.rc == 1
    assert "pre_hook must be a non-empty list of" in res.err


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


def test_meta_only_refuses_entry_kind_change(ws):
    # --meta-only moves no data and cannot migrate a kind change; recording
    # the new kind would orphan the old tree (dir->file) or publish a manifest
    # mixing both shapes (file->dir). Both directions must refuse untouched.
    import shutil

    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    shutil.rmtree(ws.root / "data")
    (ws.root / "data").write_text("now a file")

    res = ws.run("push", "--meta-only", "data")
    assert res.rc == 1
    assert "disagree on kind" in res.err
    assert "data/a.txt" in ws.keys()
    ws.run("verify", "data", expect_rc=0)  # the old backup is intact

    solo = ws.write("solo", "s")
    ws.config({"solo": {"path": str(solo)}})
    ws.run("push", "solo", expect_rc=0)
    os.remove(solo)
    ws.write("solo/inner.txt", "i")

    res = ws.run("push", "--meta-only", "solo")
    assert res.rc == 1
    assert "disagree on kind" in res.err
    ws.run("verify", "solo", expect_rc=0)


def test_first_nested_subpath_push_writes_valid_manifest(ws):
    # The first-ever manifest born from a NESTED sub-path push must record the
    # ancestor directories too: without them every later download rejects the
    # manifest ("no directory parent") and the entry is unusable.
    ws.write("data/sub/deep/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})

    ws.run("push", str(ws.root / "data" / "sub" / "deep" / "a.txt"), expect_rc=0)

    ws.run("status", "data", expect_rc=0)
    ws.run("verify", "data", expect_rc=0)
    dest = ws.root / "out"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert (dest / "sub" / "deep" / "a.txt").read_text() == "a"


def test_subpath_push_of_new_nested_directory_records_ancestors(ws):
    # A sub-path push below a directory the existing manifest predates must
    # add the ancestor records, not just the leaf's.
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.write("data/new/nested/b.txt", "b")
    ws.run("push", str(ws.root / "data" / "new" / "nested" / "b.txt"), expect_rc=0)

    ws.run("status", "data", expect_rc=0)
    ws.run("verify", "data", expect_rc=0)
    paths = _manifest_paths(ws)
    assert "./new" in paths and "./new/nested" in paths
