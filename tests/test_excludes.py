"""The exclusion predicate and its aws-cli semantics (docs/excludes.md).

Unit tests pin the `Excludes` delegation to boto3-s3's globsieve; the
workspace tests pin what a push makes of it - every path judged alone,
directories matched with a trailing slash on their key, and no propagation
in either direction.
"""

from __future__ import annotations

import json
import os

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
