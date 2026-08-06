"""The exclusion predicate and its aws-cli semantics (docs/excludes.md).

Unit tests pin the `Excludes` delegation to boto3-s3's globsieve; the
workspace tests pin what a push makes of it - every path judged alone,
directories matched with a trailing slash on their key, and no propagation
in either direction.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from s3bak.excludes import Excludes

# --- the predicate ---------------------------------------------------------


def test_bare_name_matches_file_and_symlink_keys_only():
    ex = Excludes(["cache"])
    assert ex.excluded("cache")  # a file or symlink named cache
    assert not ex.excluded("cache/")  # the directory
    assert not ex.excluded("cache/c.txt")  # its contents


def test_trailing_slash_matches_the_directory_key_only():
    ex = Excludes(["cache/"])
    assert ex.excluded("cache/")
    assert not ex.excluded("cache")
    assert not ex.excluded("cache/c.txt")


def test_dir_star_matches_the_directory_and_every_descendant():
    # The * may match the empty tail, so cache/ itself matches too - which
    # is why dir/* excludes the whole subtree without any propagation rule.
    ex = Excludes(["cache/*"])
    assert ex.excluded("cache/")
    assert ex.excluded("cache/c.txt")
    assert ex.excluded("cache/sub/")
    assert ex.excluded("cache/sub/d.txt")
    assert not ex.excluded("cache")  # a file named cache: no trailing /
    assert not ex.excluded("cachet/x")  # a sibling prefix, not a child


def test_star_spans_directory_separators():
    ex = Excludes(["*.log"])
    assert ex.excluded("a.log")
    assert ex.excluded("sub/deep/a.log")
    assert not ex.excluded("a.log/")  # a directory named a.log
    assert not ex.excluded("a.log/inside.txt")


def test_patterns_anchor_at_the_entry_root():
    ex = Excludes(["__pycache__/*"])
    assert ex.excluded("__pycache__/m.pyc")
    assert not ex.excluded("pkg/__pycache__/m.pyc")
    deep = Excludes(["*/__pycache__/*"])
    assert deep.excluded("pkg/__pycache__/m.pyc")
    assert not deep.excluded("__pycache__/m.pyc")


def test_the_entry_root_is_never_matched():
    ex = Excludes(["*"])
    assert not ex.excluded("")  # the entry root has no key
    assert ex.excluded("anything")
    assert ex.excluded("any/")


def test_absolute_pattern_matches_the_absolute_local_path():
    ex = Excludes(["/home/you/data/secret.txt"])
    assert ex.excluded("secret.txt", "/home/you/data/secret.txt")
    assert not ex.excluded("secret.txt", "/elsewhere/data/secret.txt")
    # Inert without an anchor - a manifest-only or S3-side key.
    assert not ex.excluded("secret.txt", None)


def test_prunes_subtree_only_for_the_provable_dir_star_shape():
    ex = Excludes(["cache/*", "*.log", "raw"])
    assert ex.prunes_subtree("cache/")
    assert ex.prunes_subtree("cache/sub/")
    assert not ex.prunes_subtree("cachet/")  # prefix must end at the /
    assert not ex.prunes_subtree("logs/")  # *.log cannot prove a subtree
    wild = Excludes(["ca*he/*"])
    assert not wild.prunes_subtree("cache/")  # wildcard dir part: no proof


def test_empty_pattern_list_excludes_nothing():
    ex = Excludes([])
    assert not ex.excluded("anything")
    assert not ex.prunes_subtree("any/")


# --- what a push makes of it ------------------------------------------------


def _manifest_paths(ws) -> list[str]:
    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")["Body"].read()
    return [json.loads(ln)["path"] for ln in body.decode().splitlines()[1:]]


def test_push_bare_dir_name_excludes_nothing_under_it(ws):
    # aws-cli fidelity: `--exclude cache` matches only a FILE named cache.
    # The directory and its contents are backed up in full.
    ws.write("data/cache/c.txt", "c")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache"]}})

    ws.run("push", "data", expect_rc=0)

    assert "data/cache/c.txt" in ws.keys()
    assert "./cache" in _manifest_paths(ws)


def test_push_bare_name_excludes_a_file_of_that_name(ws):
    ws.write("data/keep.txt", "k")
    ws.write("data/cache", "i am a file")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache"]}})

    ws.run("push", "data", expect_rc=0)

    assert "data/cache" not in ws.keys()
    assert "data/keep.txt" in ws.keys()


def test_push_trailing_slash_drops_only_the_directory_record(ws):
    # `cache/` matches the directory entry alone: its contents upload and
    # record normally, and the manifest simply has no ./cache record - a
    # missing parent is valid (docs/manifest.md#robustness).
    ws.write("data/cache/c.txt", "c")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/"]}})

    ws.run("push", "data", expect_rc=0)

    assert "data/cache/c.txt" in ws.keys()
    paths = _manifest_paths(ws)
    assert "./cache/c.txt" in paths
    assert "./cache" not in paths
    res = ws.run("verify", "data", expect_rc=0)
    assert "data: OK" in res.out


def test_push_dir_star_excludes_the_whole_subtree(ws):
    ws.write("data/keep.txt", "k")
    ws.write("data/cache/c.txt", "c")
    ws.write("data/cache/sub/d.txt", "d")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/*"]}})

    ws.run("push", "data", expect_rc=0)

    keys = ws.keys()
    assert "data/cache/c.txt" not in keys
    assert "data/cache/sub/d.txt" not in keys
    paths = _manifest_paths(ws)
    assert paths == [".", "./keep.txt"]


def test_push_symlink_is_not_covered_by_dir_patterns(ws):
    # A symlink named cache has the key "cache" - neither "cache/" nor
    # "cache/*" matches it, so it is recorded (its record IS its backup).
    if os.name == "nt":
        pytest.skip("symlink creation may need elevation on Windows")
    ws.write("data/keep.txt", "k")
    os.symlink("keep.txt", ws.root / "data" / "cache")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/", "cache/*"]}})

    ws.run("push", "data", expect_rc=0)

    assert "./cache" in _manifest_paths(ws)


def test_pull_restores_children_of_an_excluded_directory(ws):
    # No ./cache record (the directory is excluded): the pull creates the
    # missing level as a plain directory and restores the child.
    ws.write("data/cache/c.txt", "c")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/"]}})
    ws.run("push", "data", expect_rc=0)

    out = ws.root / "out"
    ws.run("pull", "data", "-o", str(out), expect_rc=0)

    assert (out / "cache" / "c.txt").read_text() == "c"


def test_push_absolute_pattern_excludes_by_local_path(ws):
    ws.write("data/keep.txt", "k")
    ws.write("data/skip.txt", "s")
    abs_pattern = str(ws.root / "data" / "skip.txt").replace(os.sep, "/")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": [abs_pattern]}})

    ws.run("push", "data", expect_rc=0)

    keys = ws.keys()
    assert "data/skip.txt" not in keys
    assert "data/keep.txt" in keys


# --- the ignore rule (push sub-paths and pull) -------------------------------


def test_subpath_push_of_excluded_file_is_ignored(ws):
    # The original bug: naming an excluded file used to upload it
    # unconditionally. Naming does not override the exclude - the push does
    # nothing and exits 0.
    ws.write("data/keep.txt", "k")
    ws.write("data/uv/uv-receipt.json", "r")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["uv/uv-receipt.json"]}})
    ws.run("push", "data", expect_rc=0)
    assert "data/uv/uv-receipt.json" not in ws.keys()

    res = ws.run("push", str(ws.root / "data" / "uv" / "uv-receipt.json"), expect_rc=0)

    assert res.out.strip() == ""
    assert "data/uv/uv-receipt.json" not in ws.keys()


def test_subpath_push_delete_of_excluded_file_removes_its_backup(ws, answers):
    # The bug report's scenario end to end: pushed before the exclude was
    # added, then named with --delete - the backup (object and record) goes,
    # behind the one-question subtree confirmation.
    ws.write("data/keep.txt", "k")
    ws.write("data/uv/uv-receipt.json", "r")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["uv/uv-receipt.json"]}})

    answers.feed("y")
    ws.run("push", str(ws.root / "data" / "uv" / "uv-receipt.json"), "--delete", expect_rc=0)

    assert len(answers.prompts) == 1
    assert "delete the backup subtree" in answers.prompts[0]
    assert "data/uv/uv-receipt.json" not in ws.keys()
    assert "./uv/uv-receipt.json" not in _manifest_paths(ws)
    assert (ws.root / "data" / "uv" / "uv-receipt.json").read_text() == "r"  # local untouched


def test_subpath_push_of_excluded_missing_path_is_ignored(ws):
    # Excluded AND locally missing: exclusion wins - silent exit 0 instead
    # of the missing-path error.
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["tmp/*"]}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("push", "data/tmp", expect_rc=0)

    assert res.out.strip() == "" and res.err.strip() == ""


def test_subpath_push_skips_an_excluded_ancestor_record(ws):
    # Pushing a visible path under an excluded directory records the path
    # and its visible ancestors, but not the excluded level (a parent record
    # is optional).
    ws.write("data/cache/sub/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/"]}})

    ws.run("push", "data/cache/sub", expect_rc=0)

    paths = _manifest_paths(ws)
    assert "./cache/sub/keep.txt" in paths
    assert "./cache/sub" in paths
    assert "./cache" not in paths


def test_pull_does_not_restore_an_excluded_path(ws):
    # Deleted locally and excluded: the pull leaves it deleted - the backup
    # still holds it, but the exclude makes it invisible to restore too.
    ws.write("data/keep.txt", "k")
    ws.write("data/cache/c.txt", "c")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/*"]}})
    shutil.rmtree(ws.root / "data" / "cache")
    (ws.root / "data" / "keep.txt").unlink()

    res = ws.run("pull", "data", expect_rc=0)

    assert (ws.root / "data" / "keep.txt").read_text() == "k"  # the rest restored
    assert not (ws.root / "data" / "cache").exists()
    assert "cache" not in res.out


def test_pull_does_not_overwrite_an_excluded_local_file(ws):
    ws.write("data/cache/c.txt", "recorded")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/*"]}})
    c = ws.root / "data" / "cache" / "c.txt"
    c.write_text("locally newer and longer")

    ws.run("pull", "data", expect_rc=0)

    assert c.read_text() == "locally newer and longer"


def test_pull_delete_keeps_an_excluded_local_extra(ws):
    # A never-pushed local file matching an exclude is invisible to the
    # extras diff: the mirror never offers it.
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*.log"]}})
    ws.run("push", "data", expect_rc=0)
    out = ws.root / "out"
    ws.run("pull", "data", "-o", str(out), expect_rc=0)
    extra = ws.write("out/x.log", "local only")

    ws.run("pull", "--delete", "--yes", "data", "-o", str(out), expect_rc=0)

    assert extra.read_text() == "local only"


def test_pull_of_named_excluded_file_is_ignored(ws):
    # Naming an excluded path on pull is the same silence as push: no
    # download, nothing created, exit 0.
    ws.write("data/uv/uv-receipt.json", "r")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["uv/uv-receipt.json"]}})

    dest = ws.root / "restored.json"
    res = ws.run("pull", "data/uv/uv-receipt.json", "-o", str(dest), expect_rc=0)

    assert res.out.strip() == ""
    assert not dest.exists()


def test_pull_delete_keeps_a_local_dir_with_recorded_children(ws):
    # A directory pushed while its own key was excluded has recorded
    # children but no record of its own. After lifting the exclude, pull
    # --delete must not offer (or fail on) the directory: the recorded
    # children pin it.
    ws.write("data/cache/c.txt", "c")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/"]}})
    ws.run("push", "data", expect_rc=0)
    assert "./cache" not in _manifest_paths(ws)

    ws.config({"data": {"path": str(ws.root / "data")}})  # exclude lifted
    res = ws.run("pull", "--delete", "--yes", "data", expect_rc=0)

    assert "delete" not in res.out
    assert (ws.root / "data" / "cache" / "c.txt").read_text() == "c"


def test_pull_gate_ignores_an_excluded_mismatch(ws):
    # Everything visible matches; only an excluded path drifted. The no-op
    # gate must treat the tree as settled: a --dry-run plans no metadata
    # apply and no transfers.
    ws.write("data/keep.txt", "k")
    c = ws.write("data/cache/c.txt", "c")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.run("pull", "data", expect_rc=0)  # settle metadata

    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/*"]}})
    c.write_text("locally changed and longer")

    res = ws.run("pull", "--dry-run", "data", expect_rc=0)

    assert res.out.strip() == ""


def test_pull_update_lane_vetoes_an_excluded_pair(ws):
    # Force the pull past the no-op gate with a visible difference: the
    # excluded pair must still not download, while the visible one does.
    ws.write("data/keep.txt", "k")
    c = ws.write("data/cache/c.txt", "recorded")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/*"]}})
    c.write_text("locally newer, different size")
    (ws.root / "data" / "keep.txt").unlink()  # a visible difference

    res = ws.run("pull", "data", expect_rc=0)

    assert "keep.txt" in res.out
    assert c.read_text() == "locally newer, different size"


def test_push_delete_dry_run_of_a_named_excluded_path_previews(ws, answers):
    ws.write("data/keep.txt", "k")
    ws.write("data/uv/uv-receipt.json", "r")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["uv/uv-receipt.json"]}})

    res = ws.run("push", str(ws.root / "data" / "uv" / "uv-receipt.json"), "--delete", "--dry-run")

    assert res.rc == 0
    assert answers.prompts == []
    assert "(dry-run)" in res.out and "delete" in res.out
    assert "data/uv/uv-receipt.json" in ws.keys()  # nothing actually deleted


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs an unreadable directory")
def test_subpath_push_delete_refuses_on_an_unreadable_excluded_dir(ws, answers):
    # An unreadable directory must not read as "nothing visible": treating a
    # walk gap as invisibility would hand a live backup to the one-question
    # subtree deletion. The push falls through to the normal branches, whose
    # completeness gate refuses deletions on a partial view.
    ws.write("data/cache/c.txt", "c")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/"]}})
    os.chmod(ws.root / "data" / "cache", 0)
    try:
        res = ws.run("push", "data/cache", "--delete", "--yes")
    finally:
        os.chmod(ws.root / "data" / "cache", 0o755)

    assert res.rc == 0  # cli.run maps the warnings to exit 2
    assert "kept 1 deletion candidate(s)" in res.err
    assert answers.prompts == []
    assert "data/cache/c.txt" in ws.keys()  # the backup survived the gap


# --- verify's excluded-residue warning ---------------------------------------


def test_verify_warns_about_excluded_residue(ws):
    # Records under the entry's current excludes: with every other command
    # ignoring them, verify's count is the one passive discovery channel.
    ws.write("data/keep.txt", "k")
    ws.write("data/cache/c.txt", "c")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/*"]}})
    res = ws.run("verify", "data", expect_rc=0)

    # ./cache and ./cache/c.txt both sit under the exclude.
    assert "2 recorded path(s) under excludes remain in the backup" in res.err
    assert "push --delete retires them" in res.err
    assert "1 warning(s)" in res.out


def test_verify_without_residue_stays_ok(ws):
    # An exclude nothing was ever recorded under adds no warning.
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/*"]}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("verify", "data", expect_rc=0)

    assert "under excludes" not in res.err
    assert "data: OK" in res.out


def test_verify_residue_clears_after_push_delete(ws, answers):
    ws.write("data/keep.txt", "k")
    ws.write("data/cache/c.txt", "c")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/*"]}})

    ws.run("push", "--delete", "--yes", "data", expect_rc=0)
    res = ws.run("verify", "data", expect_rc=0)

    assert "under excludes" not in res.err
    assert "data: OK" in res.out


def test_verify_unrecorded_object_under_excludes_is_not_double_counted(ws):
    # An unrecorded object under an excluded path keeps its own warning; the
    # residue count covers RECORDS only, so it must not appear here.
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/*"]}})
    ws.run("push", "data", expect_rc=0)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/cache/stray.bin", Body=b"x")

    res = ws.run("verify", "data", expect_rc=0)

    assert "unrecorded object" in res.err
    assert "under excludes" not in res.err
    assert "1 warning(s)" in res.out
