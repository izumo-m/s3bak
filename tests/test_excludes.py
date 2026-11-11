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

from s3bak.excludes import Excludes, check_patterns

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


def test_excludes_below_only_under_an_exclude_ending_in_star():
    ex = Excludes(["cache/*", "*.log", "raw"])
    assert ex.excludes_below("cache/")
    assert ex.excludes_below("cache/sub/")
    assert not ex.excludes_below("cachet/")  # prefix must end at the /
    assert not ex.excludes_below("logs/")  # *.log cannot prove a subtree
    # A trailing "*" covers whatever extends a path it matches, so a
    # wildcard before it proves the subtree too.
    assert Excludes(["*/node_modules/*"]).excludes_below("web/node_modules/")
    assert not Excludes(["*/node_modules/*"]).excludes_below("node_modules/")


def test_empty_pattern_list_excludes_nothing():
    ex = Excludes([])
    assert not ex.excluded("anything")
    assert not ex.excludes_below("any/")


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


def test_pull_delete_does_not_read_an_excluded_local_subtree(ws, monkeypatch):
    # Every walk a pull runs over the destination skips a directory
    # everything under which is excluded: nothing in it is stat'ed.
    from boto3_s3 import LocalFileGenerator

    ws.write("data/keep.txt", "k")
    ws.write("data/web/app.js", "a")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*/node_modules/*"]}})
    ws.run("push", "data", expect_rc=0)
    ws.write("data/web/node_modules/pkg/i.js", "i")
    (ws.root / "data" / "keep.txt").unlink()  # a visible difference
    statted: list[str] = []
    real = LocalFileGenerator.entry_stat_result

    def spy(self, entry):
        statted.append(entry.path)
        return real(self, entry)

    monkeypatch.setattr(LocalFileGenerator, "entry_stat_result", spy)
    ws.run("pull", "--delete", "--yes", "data", expect_rc=0)

    assert (ws.root / "data" / "keep.txt").read_text() == "k"
    assert (ws.root / "data" / "web" / "node_modules" / "pkg" / "i.js").read_text() == "i"
    assert any(path.endswith("app.js") for path in statted)
    assert not [path for path in statted if "node_modules" in path]


def test_pull_delete_keeps_an_extra_dir_holding_excluded_paths(ws, answers):
    # Removing a directory removes what it holds, so an extra directory
    # holding an excluded path stays - silently and unasked, and so does
    # every extra directory above it - while an extra directory without one
    # is removed as before. The dry run previews exactly that.
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*/node_modules/*"]}})
    ws.run("push", "data", expect_rc=0)
    ws.write("data/web/node_modules/pkg/i.js", "i")
    ws.write("data/web/app/node_modules/m.js", "m")
    ws.write("data/web/app/extra.txt", "x")
    ws.write("data/site/app/node_modules/n.js", "n")  # site holds one only below
    ws.write("data/old/stale.txt", "s")

    dry = ws.run("pull", "--delete", "--dry-run", "data", expect_rc=0)
    assert "old" in dry.out and "extra.txt" in dry.out
    assert "data/web\n" not in dry.out and "data/web/app\n" not in dry.out

    answers.feed("y", "y", "y")
    res = ws.run("pull", "--delete", "data", expect_rc=0)

    assert "delete failed" not in res.err
    assert not (ws.root / "data" / "old").exists()
    assert not (ws.root / "data" / "web" / "app" / "extra.txt").exists()
    assert (ws.root / "data" / "web" / "node_modules" / "pkg" / "i.js").read_text() == "i"
    assert (ws.root / "data" / "web" / "app" / "node_modules" / "m.js").read_text() == "m"
    assert (ws.root / "data" / "site" / "app" / "node_modules" / "n.js").read_text() == "n"
    asked = [p.split(" delete ", 1)[1].split("?")[0] for p in answers.prompts]
    root = str(ws.root / "data")
    assert asked == [f"{root}/old/stale.txt", f"{root}/old", f"{root}/web/app/extra.txt"]


def test_pull_does_not_replace_a_dir_holding_excluded_paths_with_a_symlink(ws):
    # A symlink record over a local directory replaces the directory whole -
    # unless it holds an excluded path, which pull never removes: then it is
    # a conflict, reported like a regular-file record over a directory.
    ws.write("data/keep.txt", "k")
    os.symlink("keep.txt", ws.root / "data" / "x")
    ws.write("data/y/plain.txt", "p")
    os.symlink("keep.txt", ws.root / "data" / "z")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    os.remove(ws.root / "data" / "x")
    ws.write("data/x/a/b/cache.secret", "excluded content")  # found only by looking below
    os.remove(ws.root / "data" / "z")
    ws.write("data/z/notes.txt", "unrecorded, not excluded")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*.secret"]}})

    dry = ws.run("pull", "--dry-run", "data", expect_rc=1)
    assert "type conflict: a directory holding excluded paths" in dry.err
    assert "data/z" not in dry.err
    res = ws.run("pull", "data", expect_rc=1)

    assert "type conflict: a directory holding excluded paths" in res.err
    assert "records a symlink" in res.err and "data/x" in res.err
    assert (ws.root / "data" / "x" / "a" / "b" / "cache.secret").read_text() == "excluded content"
    # A directory holding nothing excluded is still replaced, as documented.
    assert os.path.islink(ws.root / "data" / "z")


def test_subpath_pull_does_not_replace_a_dir_holding_excluded_paths(ws):
    # The named path is the restore root, replaced whole when it is of the
    # wrong type - but not when it holds an excluded path. The dry run says
    # the same before anything moves.
    ws.write("data/keep.txt", "k")
    ws.write("data/x", "recorded file")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*.log"]}})
    ws.run("push", "data", expect_rc=0)
    os.remove(ws.root / "data" / "x")
    ws.write("data/x/a/b/run.log", "excluded content")  # found only by looking below
    x = ws.root / "data" / "x"

    dry = ws.run("pull", "--dry-run", str(x), expect_rc=1)
    assert "type conflict: a directory holding excluded paths" in dry.err
    res = ws.run("pull", str(x), expect_rc=1)

    assert "records a regular file" in res.err
    assert (x / "a" / "b" / "run.log").read_text() == "excluded content"
    assert not [p for p in os.listdir(ws.root / "data") if ".s3bak-stage-" in p]


def test_holds_excluded_looks_below_only_when_recursive(tmp_path):
    from s3bak.restore import holds_excluded

    (tmp_path / "d" / "sub" / "node_modules").mkdir(parents=True)
    (tmp_path / "d" / "plain.txt").write_text("p")
    ex = Excludes(["*/node_modules/*"])
    anchor = str(tmp_path / "d").replace(os.sep, "/") + "/"
    d = str(tmp_path / "d")
    assert holds_excluded(ex, d, "d/", anchor, recursive=False) is False
    assert holds_excluded(ex, d, "d/", anchor, recursive=True)
    # The directory itself counts.
    assert holds_excluded(Excludes(["d/"]), d, "d/", anchor, recursive=False)
    assert holds_excluded(Excludes([]), d, "d/", anchor, recursive=True) is False


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs an unreadable directory")
def test_holds_excluded_cannot_clear_an_unreadable_dir(tmp_path):
    from s3bak.restore import holds_excluded

    (tmp_path / "d" / "locked").mkdir(parents=True)
    os.chmod(tmp_path / "d" / "locked", 0)
    ex = Excludes(["*.log"])
    anchor = str(tmp_path / "d").replace(os.sep, "/") + "/"
    # Nothing shows it holds none: neither True nor False.
    try:
        assert holds_excluded(ex, str(tmp_path / "d"), "d/", anchor, recursive=True) is None
        locked = str(tmp_path / "d" / "locked")
        assert holds_excluded(ex, locked, "d/locked/", anchor + "locked/", recursive=False) is None
    finally:
        os.chmod(tmp_path / "d" / "locked", 0o755)


def test_holds_excluded_keys_a_junction_as_a_directory_without_entering_it(tmp_path, monkeypatch):
    # Windows, simulated (only d/link is a junction): the walk keys a
    # junction as a directory ("link/"), so an exclude written for its path
    # ("d/link/*") must see it; and removing d only unlinks it, so a
    # replacement does not look behind it.
    from s3bak import restore

    (tmp_path / "d" / "link").mkdir(parents=True)
    (tmp_path / "d" / "link" / "inside.log").write_text("x")
    link_ino = os.lstat(tmp_path / "d" / "link").st_ino
    monkeypatch.setattr(restore, "IS_WINDOWS", True)
    monkeypatch.setattr(restore, "is_junction", lambda st: st.st_ino == link_ino)
    anchor = str(tmp_path / "d").replace(os.sep, "/") + "/"
    d = str(tmp_path / "d")
    assert restore.holds_excluded(Excludes(["d/link/*"]), d, "d/", anchor, recursive=False)
    assert restore.holds_excluded(Excludes(["*.log"]), d, "d/", anchor, recursive=True) is False
    # Without the junction, the same search finds it.
    monkeypatch.setattr(restore, "is_junction", lambda st: False)
    assert restore.holds_excluded(Excludes(["*.log"]), d, "d/", anchor, recursive=True) is True


def test_remove_extras_still_warns_about_an_aliased_dir_holding_excluded_paths(tmp_path, capfd):
    # The name-folding check comes first: a directory that may be the
    # recorded one under another spelling is warned about whatever it holds.
    from s3bak import restore

    d = tmp_path / "a" / "Docs"
    d.mkdir(parents=True)
    items = [("a/Docs", str(d), True, True)]
    aliases = {("a", restore.fs_alias_key("docs"))}
    errors, removed = restore.remove_extras(
        iter(items), aliases=aliases, dir_holds_excluded=lambda rel, path: True
    )

    assert (errors, removed) == (0, 0)
    assert "not removed" in capfd.readouterr().err


def test_pull_u_refused_symlink_leaves_its_parents_newer_mtime(ws):
    # Under -u a refused placement must not flag its parent for the
    # post-placement re-settle: a parent the pull did not touch keeps a
    # newer local modification time.
    ws.write("data/keep.txt", "k")
    ws.write("data/sub/inner.txt", "i")
    os.symlink("../keep.txt", ws.root / "data" / "sub" / "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    x = ws.root / "data" / "sub" / "x"
    os.remove(x)
    ws.write("data/sub/x/cache.secret", "excluded content")
    os.utime(x, (1, 1))  # older than the symlink record: the record wins under -u
    sub = ws.root / "data" / "sub"
    newer = os.stat(sub).st_mtime_ns + 3600 * 10**9
    os.utime(sub, ns=(newer, newer))
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*.secret"]}})

    dry = ws.run("pull", "-u", "--dry-run", "data", expect_rc=1)
    assert "type conflict: a directory holding excluded paths" in dry.err
    res = ws.run("pull", "-u", "data", expect_rc=1)

    assert "type conflict: a directory holding excluded paths" in res.err
    assert (x / "cache.secret").read_text() == "excluded content"
    assert os.stat(sub).st_mtime_ns == newer


def _symlink_over_dir_workspace(ws, excludes):
    """A pushed entry whose ./x is a symlink, with a local directory at x now
    holding x/cache.secret."""
    ws.write("data/keep.txt", "k")
    os.symlink("keep.txt", ws.root / "data" / "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    os.remove(ws.root / "data" / "x")
    ws.write("data/x/cache.secret", "excluded content")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": excludes}})
    return ws.root / "data" / "x"


def test_pull_u_keeps_a_newer_dir_holding_excluded_paths_without_a_conflict(ws):
    # Under -u a directory newer than the symlink record is kept as the
    # newer side: no placement, so no conflict - the dry run agrees.
    x = _symlink_over_dir_workspace(ws, ["*.secret"])
    later = os.stat(x).st_mtime_ns + 3600 * 10**9
    os.utime(x, ns=(later, later))

    dry = ws.run("pull", "-u", "--dry-run", "data", expect_rc=0)
    res = ws.run("pull", "-u", "data", expect_rc=0)

    assert "type conflict" not in dry.err + res.err
    assert (x / "cache.secret").read_text() == "excluded content"


def test_pull_skips_an_excluded_symlink_record_over_a_dir(ws):
    # An excluded record is not the pull's: no placement, no conflict.
    x = _symlink_over_dir_workspace(ws, ["x", "*.secret"])

    dry = ws.run("pull", "--dry-run", "data", expect_rc=0)
    res = ws.run("pull", "data", expect_rc=0)

    assert "type conflict" not in dry.err + res.err
    assert (x / "cache.secret").read_text() == "excluded content"


def test_subpath_pull_of_a_symlink_record_over_a_dir_holding_excluded_paths(ws):
    # A symlink sub-path has no download; the apply places it and refuses
    # the directory the same way, and the dry run says so first.
    x = _symlink_over_dir_workspace(ws, ["*.secret"])

    dry = ws.run("pull", "--dry-run", str(x), expect_rc=1)
    res = ws.run("pull", str(x), expect_rc=1)

    assert "type conflict: a directory holding excluded paths" in dry.err
    assert "type conflict: a directory holding excluded paths" in res.err
    assert (x / "cache.secret").read_text() == "excluded content"


def test_pull_dry_run_finds_no_conflict_behind_a_replaced_root(ws):
    # The restore root is a symlink, which a real pull replaces by a fresh
    # stage: no local directory is in the symlink record's way there, so
    # the dry run, which still sees through the old root, must not report
    # one either.
    ws.write("data/keep.txt", "k")
    os.symlink("keep.txt", ws.root / "data" / "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    os.rename(ws.root / "data", ws.root / "elsewhere")
    os.remove(ws.root / "elsewhere" / "x")
    ws.write("elsewhere/x/cache.secret", "excluded content")
    os.symlink("elsewhere", ws.root / "data")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*.secret"]}})

    dry = ws.run("pull", "--dry-run", "data", expect_rc=0)
    res = ws.run("pull", "data", expect_rc=0)

    assert "type conflict" not in dry.err + res.err
    assert (ws.root / "elsewhere" / "x" / "cache.secret").read_text() == "excluded content"


def test_pull_dry_run_finds_no_conflict_through_a_symlinked_recorded_dir(ws):
    # A symlink at a recorded directory is replaced by a real directory
    # before the sync, so the symlink record under it meets no directory in
    # the real run; the dry run must not look through the link either.
    ws.write("data/keep.txt", "k")
    ws.write("data/a/plain.txt", "p")
    os.symlink("../keep.txt", ws.root / "data" / "a" / "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    shutil.rmtree(ws.root / "data" / "a")
    ws.write("outside/x/cache.secret", "excluded content")
    os.symlink(str(ws.root / "outside"), ws.root / "data" / "a")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*.secret"]}})

    dry = ws.run("pull", "--dry-run", "data", expect_rc=0)

    assert "type conflict" not in dry.err


def test_pull_dry_run_finds_no_conflict_through_an_in_tree_symlinked_recorded_dir(ws):
    # The same with the link pointing inside the tree: what it leads to is a
    # recorded path of its own, but the record under the link meets no
    # directory in the real run, which replaces the link first.
    ws.write("data/keep.txt", "k")
    ws.write("data/a/plain.txt", "p")
    os.symlink("../keep.txt", ws.root / "data" / "a" / "x")
    ws.write("data/c/x/inner.txt", "i")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    shutil.rmtree(ws.root / "data" / "a")
    os.symlink("c", ws.root / "data" / "a")
    ws.write("data/c/x/cache.secret", "excluded content")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*.secret"]}})

    dry = ws.run("pull", "--dry-run", "data", expect_rc=0)
    res = ws.run("pull", "data", expect_rc=0)

    assert "type conflict" not in dry.err + res.err
    assert (ws.root / "data" / "c" / "x" / "cache.secret").read_text() == "excluded content"


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs an unreadable directory")
def test_pull_does_not_replace_a_dir_it_cannot_read_in_full(ws):
    # A replacement moves the directory away before removing it, so a part
    # it cannot read would leave its place unseen: refused, in its own words.
    x = _symlink_over_dir_workspace(ws, ["*.secret"])
    os.remove(x / "cache.secret")
    (x / "locked").mkdir()
    os.chmod(x / "locked", 0)
    try:
        dry = ws.run("pull", "--dry-run", "data", expect_rc=1)
        res = ws.run("pull", "data", expect_rc=1)
    finally:
        os.chmod(x / "locked", 0o755)

    assert "type conflict: a directory that could not be read in full" in dry.err
    assert "type conflict: a directory that could not be read in full" in res.err
    assert (x / "locked").is_dir()


def test_holds_excluded_has_no_depth_limit(tmp_path):
    import inspect
    import sys

    from s3bak.restore import holds_excluded

    # Deeper than the recursion limit is set to below, so a search that
    # takes a frame per level fails; built and removed level by level, since
    # os.makedirs, shutil.rmtree and pytest's own temp cleanup recurse.
    depth = 200
    base = str(tmp_path / "d")
    deep = base
    limit = sys.getrecursionlimit()
    try:
        os.mkdir(deep)
        for _ in range(depth):
            deep = os.path.join(deep, "a")
            os.mkdir(deep)
        with open(os.path.join(deep, "x.secret"), "w") as f:
            f.write("s")
        anchor = base.replace(os.sep, "/") + "/"
        ex = Excludes(["*.secret"])
        sys.setrecursionlimit(len(inspect.stack()) + depth // 2)
        try:
            assert holds_excluded(ex, base, "d/", anchor, recursive=True) is True
        finally:
            sys.setrecursionlimit(limit)
    finally:
        secret = os.path.join(deep, "x.secret")
        if os.path.exists(secret):
            os.remove(secret)
        while os.path.isdir(deep):
            os.rmdir(deep)
            if deep == base:
                break
            deep = os.path.dirname(deep)


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


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo on this platform")
def test_pull_does_not_warn_about_an_excluded_special_file(ws):
    # The destination walk vets special files while it scans, before the
    # lanes judge the key; an excluded one must still pass in silence - a
    # top-level one and one under an excluded directory alike.
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cm-*", "agent/*"]}})
    os.mkfifo(ws.root / "data" / "cm-sock")
    (ws.root / "data" / "agent").mkdir()
    os.mkfifo(ws.root / "data" / "agent" / "s.sock")
    os.mkfifo(ws.root / "data" / "visible-fifo")
    (ws.root / "data" / "keep.txt").unlink()  # a visible difference

    res = ws.run("pull", "data", expect_rc=0)

    assert (ws.root / "data" / "keep.txt").read_text() == "k"
    assert "cm-sock" not in res.err
    assert "s.sock" not in res.err
    # A special file the excludes do not cover is still reported.
    assert "Skipping file" in res.err and "visible-fifo" in res.err


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo on this platform")
def test_subpath_pull_judges_special_files_at_the_entry_root(ws):
    # A sub pull walks the sub, but the patterns stay entry-rooted.
    ws.write("data/sub/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["sub/cm-*"]}})
    os.mkfifo(ws.root / "data" / "sub" / "cm-sock")
    (ws.root / "data" / "sub" / "keep.txt").unlink()

    res = ws.run("pull", str(ws.root / "data" / "sub"), expect_rc=0)

    assert (ws.root / "data" / "sub" / "keep.txt").read_text() == "k"
    assert "cm-sock" not in res.err


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


def test_verify_dir_sub_counts_its_own_excluded_record(ws):
    # The sub's own record never enters the sub-scoped record stream, so its
    # residue judgment is seeded separately - `verify entry/cache` must not
    # read as a false OK while `verify entry` warns about the same record.
    ws.write("data/cache/c.txt", "c")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/"]}})

    res = ws.run("verify", "data/cache", expect_rc=0)

    assert "1 recorded path(s) under excludes remain in the backup" in res.err

    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/*"]}})
    res = ws.run("verify", "data/cache", expect_rc=0)
    # The dir's own record plus c.txt, both inside the narrowed range.
    assert "2 recorded path(s) under excludes remain in the backup" in res.err


def test_verify_named_excluded_file_sub_counts_one(ws):
    ws.write("data/uv/uv-receipt.json", "r")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["uv/uv-receipt.json"]}})

    res = ws.run("verify", "data/uv/uv-receipt.json", expect_rc=0)

    assert "1 recorded path(s) under excludes remain in the backup" in res.err


def test_verify_counts_residue_next_to_an_unrecorded_object(ws):
    # A residue record and an unrecorded object under the same exclude:
    # each keeps its own warning, and the residue count covers records only.
    ws.write("data/keep.txt", "k")
    ws.write("data/cache/c.txt", "c")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/*"]}})
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/cache/stray.bin", Body=b"x")

    res = ws.run("verify", "data", expect_rc=0)

    assert "unrecorded object" in res.err
    assert "2 recorded path(s) under excludes remain in the backup" in res.err
    assert "2 warning(s)" in res.out


def test_verify_checksum_skips_residue_content(ws):
    # A residue file whose local copy diverged at the same size: the content
    # check must not turn it into an unfixable error - push --checksum
    # cannot upload an excluded path. The residue warning stands alone.
    c = ws.write("data/cache/c.txt", "abc")
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/*"]}})
    recorded = os.lstat(c)
    c.write_text("xyz")  # same size, different content
    os.utime(c, ns=(recorded.st_mtime_ns, recorded.st_mtime_ns))

    res = ws.run("verify", "--checksum", "data", expect_rc=0)

    assert "content differs" not in res.err
    assert "0 error(s)" in res.out
    assert "under excludes remain in the backup" in res.err


# --- includes: a "!" pattern takes a path back ------------------------------


def test_a_bang_pattern_takes_a_path_back():
    # The aws-cli idiom --exclude "*" --include "*.md".
    ex = Excludes(["*", "!*.md"])
    assert not ex.excluded("notes.md")
    assert not ex.excluded("docs/deep/notes.md")
    assert ex.excluded("notes.txt")
    assert ex.excluded("docs/")  # a directory key ends in "/", not ".md"


def test_the_last_matching_pattern_decides():
    ex = Excludes(["*", "!.config/*", ".config/Code/Cache/*", "!.config/Code/Cache/keep"])
    assert ex.excluded("Downloads/big.iso")
    assert not ex.excluded(".config/")
    assert not ex.excluded(".config/nvim/init.lua")
    assert ex.excluded(".config/Code/Cache/")
    assert ex.excluded(".config/Code/Cache/blob")
    assert not ex.excluded(".config/Code/Cache/keep")


def test_an_include_before_the_exclude_is_overridden():
    # Order is aws-cli's: a later exclude wins over an earlier include, so
    # the same two patterns mean different things in the two orders.
    assert not Excludes(["cache/*", "!cache/keep/*"]).excluded("cache/keep/k.txt")
    assert Excludes(["!cache/keep/*", "cache/*"]).excluded("cache/keep/k.txt")


def test_includes_do_not_propagate_to_ancestors():
    # Every path is judged alone in both directions: taking back docs/*.md
    # takes back no directory key. "!*/" is the idiom that records them.
    ex = Excludes(["*", "!docs/*.md"])
    assert not ex.excluded("docs/a.md")
    assert ex.excluded("docs/")
    with_dirs = Excludes(["*", "!*/", "!docs/*.md"])
    assert not with_dirs.excluded("docs/")
    assert not with_dirs.excluded("other/")
    assert with_dirs.excluded("other/x.txt")


def test_absolute_include_matches_the_absolute_local_path():
    ex = Excludes(["*", "!/home/you/data/keep.txt"])
    assert not ex.excluded("keep.txt", "/home/you/data/keep.txt")
    assert ex.excluded("keep.txt", "/elsewhere/data/keep.txt")
    assert ex.excluded("keep.txt", None)  # inert without an anchor


def test_a_literal_leading_bang_is_spelled_as_a_character_class():
    ex = Excludes([r"[\!]important"])
    assert ex.excluded("!important")
    assert not ex.excluded("important")


def test_prune_needs_no_later_include_under_the_directory():
    ex = Excludes(["*", "!.config/nvim/*", "!.bashrc"])
    assert ex.excludes_below("Downloads/")
    assert ex.excludes_below(".config/Code/")
    assert not ex.excludes_below(".config/")
    assert not ex.excludes_below(".config/nvim/")
    assert not ex.excludes_below(".config/nvim/lua/")
    # Conservative: an include whose literal head is a prefix of the path
    # may reach it, even when it has no wildcard to reach with.
    assert not ex.excludes_below(".bashrc.d/")


def test_prune_is_blocked_by_an_include_that_might_reach_the_subtree():
    assert not Excludes(["cache/*", "!*.keep"]).excludes_below("cache/")
    carved = Excludes(["cache/*", "!cache/keep/*"])
    assert not carved.excludes_below("cache/")
    assert carved.excludes_below("cache/other/")


def test_prune_follows_the_last_covering_pattern():
    ex = Excludes(["*", "!docs/*", "docs/tmp/*"])
    assert not ex.excludes_below("docs/")
    assert ex.excludes_below("docs/tmp/")
    assert ex.excludes_below("src/")
    # An include the covering exclude overrides does not block the prune.
    assert Excludes(["!cache/keep/*", "cache/*"]).excludes_below("cache/")


def test_prune_judges_an_absolute_include_by_the_directory_path():
    ex = Excludes(["*", "!/home/you/data/keep/*"])
    assert not ex.excludes_below("keep/", "/home/you/data/keep/")
    assert ex.excludes_below("other/", "/home/you/data/other/")
    # Without an anchor, absolute patterns are inert, as they are in excluded.
    assert ex.excludes_below("other/")


def test_check_patterns_rejects_a_bare_bang_and_a_leading_include():
    assert check_patterns([]) is None
    assert check_patterns(["*", "!*.md"]) is None
    assert "bare '!'" in (check_patterns(["*", "!"]) or "")
    assert "empty pattern" in (check_patterns(["*", ""]) or "")
    assert "must not start with an include" in (check_patterns(["!*.md", "*"]) or "")


def test_push_include_takes_back_files_under_a_catch_all_exclude(ws):
    # Only the .md files are backed up. Directory keys match no include, so
    # no directory is recorded - a pull creates them as plain directories.
    ws.write("data/notes.md", "n")
    ws.write("data/docs/deep/a.md", "a")
    ws.write("data/docs/b.txt", "b")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*", "!*.md"]}})

    ws.run("push", "data", expect_rc=0)

    keys = ws.keys()
    assert "data/notes.md" in keys and "data/docs/deep/a.md" in keys
    assert "data/docs/b.txt" not in keys
    assert set(_manifest_paths(ws)) == {".", "./notes.md", "./docs/deep/a.md"}


def test_push_bang_slash_idiom_records_the_directories(ws):
    ws.write("data/docs/a.md", "a")
    ws.write("data/docs/b.txt", "b")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*", "!*/", "!*.md"]}})

    ws.run("push", "data", expect_rc=0)

    assert set(_manifest_paths(ws)) == {".", "./docs", "./docs/a.md"}


def test_push_include_carves_a_subtree_back_out_of_an_exclude(ws):
    ws.write("data/cache/junk.bin", "j")
    ws.write("data/cache/keep/k.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/*", "!cache/keep/*"]}})

    ws.run("push", "data", expect_rc=0)

    keys = ws.keys()
    assert "data/cache/keep/k.txt" in keys
    assert "data/cache/junk.bin" not in keys
    paths = _manifest_paths(ws)
    assert "./cache/keep" in paths and "./cache" not in paths


def test_subpath_push_of_a_dir_with_an_included_descendant_pushes_it(ws):
    # The directory's own key is excluded, but the walk beneath it is not
    # empty: naming it pushes what the includes take back.
    ws.write("data/.config/nvim/init.lua", "i")
    ws.write("data/.config/Code/Cache/blob", "b")
    ws.write("data/Downloads/big.iso", "x")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*", "!.config/nvim/*"]}})

    ws.run("push", "data/.config", expect_rc=0)

    keys = ws.keys()
    assert "data/.config/nvim/init.lua" in keys
    assert "data/.config/Code/Cache/blob" not in keys
    assert "data/Downloads/big.iso" not in keys
    paths = _manifest_paths(ws)
    assert "./.config/nvim/init.lua" in paths and "./.config/nvim" in paths
    assert "./.config" not in paths


def test_pull_restores_only_what_the_includes_take_back(ws):
    ws.write("data/keep.md", "k")
    ws.write("data/skip.txt", "s")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*", "!*.md"]}})
    out = ws.root / "out"
    ws.run("pull", "data", "-o", str(out), expect_rc=0)

    assert (out / "keep.md").read_text() == "k"
    assert not (out / "skip.txt").exists()


def test_verify_counts_the_records_the_includes_do_not_take_back(ws):
    ws.write("data/keep.md", "k")
    ws.write("data/skip.txt", "s")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*", "!*.md"]}})
    res = ws.run("verify", "data", expect_rc=0)

    assert "1 recorded path(s) under excludes remain in the backup" in res.err


def test_excludes_below_folds_names_as_the_matching_does(monkeypatch):
    # Windows, simulated (globsieve reads _WINDOWS when a filter is
    # compiled): matching ignores case there, so "!docs/*" takes "Docs/"
    # back, and the skip must not drop what the matching keeps.
    from boto3_s3 import globsieve

    monkeypatch.setattr(globsieve, "_WINDOWS", True)
    ex = Excludes(["*", "!docs/*"])
    assert not ex.excluded("Docs/a.md", "C:/data/Docs/a.md")
    assert not ex.excludes_below("Docs/", "C:/data/Docs/")
    assert ex.excludes_below("src/", "C:/data/src/")


def test_subpath_push_of_an_absent_dir_the_config_takes_back_is_missing(ws):
    # Absent locally, the named path is judged by what the backup records
    # under it: x/ and x/a.md are taken back, so this is a missing path -
    # the guard against a silent no-op stays - not an ignored one.
    ws.write("data/x/a.md", "a")
    ws.write("data/skip.txt", "s")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*", "!x/*"]}})
    ws.run("push", "data", expect_rc=0)
    shutil.rmtree(ws.root / "data" / "x")

    res = ws.run("push", "data/x", expect_rc=1)

    assert "local path does not exist" in res.err
    assert "data/x/a.md" in ws.keys()


def test_subpath_push_of_an_absent_unrecorded_name_under_a_catch_all_is_ignored(ws):
    # Nothing recorded and nothing local: no kind to judge by, so the name
    # counts as excluded when either spelling is - and under "*" both are.
    ws.write("data/x/a.md", "a")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*", "!x/*"]}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("push", "data/nope", expect_rc=0)

    assert res.out.strip() == "" and res.err.strip() == ""


def test_subpath_push_of_an_absent_dir_with_only_excluded_records_is_ignored(ws):
    # Residue from before the exclude was added: every record under the name
    # is excluded, so exclusion wins over "missing" (push --delete retires it).
    ws.write("data/cache/c.txt", "c")
    ws.write("data/keep.txt", "k")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["cache/*"]}})
    shutil.rmtree(ws.root / "data" / "cache")

    res = ws.run("push", "data/cache", expect_rc=0)

    assert res.out.strip() == "" and res.err.strip() == ""
    assert "data/cache/c.txt" in ws.keys()
