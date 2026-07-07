"""Manifest v3: JSONL parse/format, S3-key-order walk, and subtree patching."""

from __future__ import annotations

import io
import json
import os
import sys

import pytest

from s3bak import manifest

# --- format / parse -----------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "myfile.txt",
        "notes -> archive",
        "a' -> 'b",
        "it's a file.txt",
        'quo"ted',
        "日本語.txt",
        "a\nb.txt",  # JSONL escapes newlines; v2's line format could not
    ],
)
def test_format_parse_roundtrip_tricky_names(tmp_path, name):
    p = tmp_path / "f"
    p.write_text("x")
    st = os.lstat(p)
    line = manifest.format_entry(f"./{name}", st, None)
    assert "\n" not in line  # one entry stays one line, whatever the name
    e = manifest.parse_entry(line)
    assert e is not None
    assert e.path == f"./{name}"
    assert e.size == 1
    assert e.mtime_ns == st.st_mtime_ns
    assert e.is_file and not e.is_dir


def test_symlink_entry_records_target_and_no_size(tmp_path):
    os.symlink("target -> x", tmp_path / "link")
    st = os.lstat(tmp_path / "link")
    e = manifest.parse_entry(manifest.format_entry("./link", st, "target -> x"))
    assert e is not None
    assert e.sym_target == "target -> x"
    assert not e.is_file and not e.is_dir
    assert e.size is None


def test_directory_entry_records_full_mode(tmp_path):
    os.chmod(tmp_path, 0o755)
    st = os.lstat(tmp_path)
    e = manifest.parse_entry(manifest.format_entry(".", st, None))
    assert e is not None
    assert e.is_dir
    assert e.perm_str == "755"


def test_parse_entry_ignores_unknown_keys():
    line = '{"path":"./a","mode":"100644","size":1,"mtime_ns":5,"future_key":"?"}'
    e = manifest.parse_entry(line)
    assert e is not None
    assert e.path == "./a"


def test_parse_entry_rejects_damage():
    assert manifest.parse_entry("not json") is None
    assert manifest.parse_entry('{"mode":"100644"}') is None  # no path
    assert manifest.parse_entry('{"path":"./a","mode":"notoctal"}') is None
    assert manifest.parse_entry("") is None


# --- header / iter_manifest ---------------------------------------------------


def test_iter_manifest_requires_header(tmp_path):
    p = tmp_path / "m.jsonl"
    p.write_text('{"path":".","mode":"40755","mtime_ns":0}\n')
    with pytest.raises(manifest.ManifestError):
        list(manifest.iter_manifest(str(p)))


def test_iter_manifest_rejects_future_version(tmp_path):
    p = tmp_path / "m.jsonl"
    p.write_text('{"s3bak_manifest":4}\n')
    with pytest.raises(manifest.ManifestError):
        list(manifest.iter_manifest(str(p)))


def test_iter_manifest_skips_damaged_lines(tmp_path):
    p = tmp_path / "m.jsonl"
    p.write_text(
        '{"s3bak_manifest":3}\ngarbage\n{"path":"./a","mode":"100644","size":1,"mtime_ns":5}\n'
    )
    assert [e.path for e in manifest.iter_manifest(str(p))] == ["./a"]


# --- size+mtime check ----------------------------------------------------------


def test_matches_stat_window(tmp_path):
    p = tmp_path / "f"
    p.write_text("x")
    st = os.lstat(p)
    e = manifest.parse_entry(manifest.format_entry("./f", st, None))
    assert e is not None
    assert e.matches_stat(st, 0)  # exact
    os.utime(p, ns=(st.st_mtime_ns + 10**9, st.st_mtime_ns + 10**9))  # +1s
    drifted = os.lstat(p)
    assert e.matches_stat(drifted, 2_000_000_000)  # within the 2s window
    assert not e.matches_stat(drifted, 0)  # strict


# --- walk order ----------------------------------------------------------------


def test_walk_tree_is_in_s3_key_order(tmp_path):
    # "foo.txt" must sort BEFORE the "foo/" subtree ('.' < '/'), and a dir's
    # record must stand immediately before its children.
    (tmp_path / "foo").mkdir()
    (tmp_path / "foo" / "bar").write_text("b")
    (tmp_path / "foo.txt").write_text("f")
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / ".hidden").write_text("h")

    rels = [rel for rel, _st, _sym in manifest.walk_tree(str(tmp_path), [])]
    assert rels == [".", "./.hidden", "./a.txt", "./foo.txt", "./foo", "./foo/bar"]


def test_walk_tree_applies_excludes(tmp_path):
    (tmp_path / "keep.txt").write_text("k")
    (tmp_path / "skip.log").write_text("s")
    (tmp_path / "pruned").mkdir()
    (tmp_path / "pruned" / "x").write_text("x")

    rels = [rel for rel, _st, _sym in manifest.walk_tree(str(tmp_path), ["*.log", "pruned/*"])]
    assert rels == [".", "./keep.txt"]


def test_path_match_negated_class():
    # Glob negation is '!'; regex would read a verbatim '[!a]' as a class
    # holding the literal '!' - the exact inverse (matching 'a', missing 'b').
    # The translator must emit '^'.
    assert manifest.path_match("b", "[!a]")
    assert not manifest.path_match("a", "[!a]")


def test_path_match_unterminated_class_is_literal():
    # fnmatch behavior: an unterminated '[' matches itself rather than
    # crashing the regex compile.
    assert manifest.path_match("x[", "x[")
    assert not manifest.path_match("x", "x[")


def _stack_depth() -> int:
    d, f = 0, sys._getframe()
    while f is not None:
        d += 1
        f = f.f_back
    return d


def test_walk_tree_is_iterative_not_recursion_bound(tmp_path):
    # Prove the walk does not consume a stack frame per directory level: under
    # a recursion limit set just above the current depth, a tree far deeper
    # than that headroom would overflow a recursive `yield from` walk but must
    # not overflow this iterative one. 300 dirs keeps I/O and teardown cheap
    # (rmtree stays well under its own limit, so no landmine is left behind).
    depth = 300
    root = tmp_path / "deep"
    root.mkdir()
    p = root
    for _ in range(depth):
        p = p / "d"
        os.mkdir(p)
    (p / "f.txt").write_text("x")

    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(_stack_depth() + 50)  # < depth: a per-level recursion overflows
    try:
        count = sum(1 for _ in manifest.walk_tree(str(root), []))
    finally:
        sys.setrecursionlimit(old_limit)
    assert count == depth + 2  # root + each dir + the file


def test_walk_tree_skips_unreadable_directory(tmp_path):
    # An unreadable subdirectory is silently skipped (as os.walk did): its own
    # record is kept, its children are not, and the walk does not raise.
    (tmp_path / "good.txt").write_text("g")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "x").write_text("x")
    os.chmod(locked, 0)
    try:
        rels = [rel for rel, _st, _sym in manifest.walk_tree(str(tmp_path), [])]
    finally:
        os.chmod(locked, 0o755)
    assert rels == [".", "./good.txt", "./locked"]


def test_entry_sort_key():
    assert manifest.entry_sort_key(".", True) == ""  # root always first
    assert manifest.entry_sort_key("./foo", True) == "foo/"
    assert manifest.entry_sort_key("./foo.txt", False) == "foo.txt"
    assert manifest.entry_sort_key("solo.txt", False) == "solo.txt"


# --- subtree patch -------------------------------------------------------------


def _manifest_text(lines: list[str]) -> str:
    return "\n".join(['{"s3bak_manifest":3}', *lines]) + "\n"


def test_write_patched_replaces_subtree_in_order(tmp_path):
    # Old manifest holds ., a.txt, sub.txt, sub/, sub/old.txt, z.txt. Patch
    # `sub` from a local tree now holding new.txt. "sub.txt" (key 'sub.')
    # interleaves BETWEEN the file key 'sub' and the dir key 'sub/'.
    old = tmp_path / "old.jsonl"
    old.write_text(
        _manifest_text(
            [
                '{"path":".","mode":"40755","mtime_ns":0}',
                '{"path":"./a.txt","mode":"100644","size":1,"mtime_ns":0}',
                '{"path":"./sub.txt","mode":"100644","size":1,"mtime_ns":0}',
                '{"path":"./sub","mode":"40755","mtime_ns":0}',
                '{"path":"./sub/old.txt","mode":"100644","size":1,"mtime_ns":0}',
                '{"path":"./z.txt","mode":"100644","size":1,"mtime_ns":0}',
            ]
        )
    )
    local_sub = tmp_path / "sub"
    local_sub.mkdir()
    (local_sub / "new.txt").write_text("n")

    out = io.StringIO()
    manifest.write_patched(out, str(old), "sub", manifest.iter_subtree(str(local_sub), "sub", []))
    lines = out.getvalue().splitlines()
    assert json.loads(lines[0]) == {"s3bak_manifest": 3}
    rels = [json.loads(ln)["path"] for ln in lines[1:]]
    assert rels == [".", "./a.txt", "./sub.txt", "./sub", "./sub/new.txt", "./z.txt"]


def test_write_patched_removes_deleted_subtree(tmp_path):
    old = tmp_path / "old.jsonl"
    old.write_text(
        _manifest_text(
            [
                '{"path":".","mode":"40755","mtime_ns":0}',
                '{"path":"./gone","mode":"40755","mtime_ns":0}',
                '{"path":"./gone/x","mode":"100644","size":1,"mtime_ns":0}',
                '{"path":"./keep.txt","mode":"100644","size":1,"mtime_ns":0}',
            ]
        )
    )
    out = io.StringIO()
    manifest.write_patched(out, str(old), "gone", [])
    rels = [json.loads(ln)["path"] for ln in out.getvalue().splitlines()[1:]]
    assert rels == [".", "./keep.txt"]


# --- on-the-wire format --------------------------------------------------------


def test_push_writes_v3_manifest(ws):
    ws.write("data/a.txt", "alpha")
    ws.write("data/sub/b.txt", "beta")
    os.symlink("a.txt", ws.root / "data" / "link")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl")["Body"].read()
    lines = body.decode("utf-8").splitlines()
    assert json.loads(lines[0]) == {"s3bak_manifest": 3}
    entries = [json.loads(ln) for ln in lines[1:]]

    rels = [e["path"] for e in entries]
    assert rels == [".", "./a.txt", "./link", "./sub", "./sub/b.txt"]

    root = entries[0]
    assert root["mode"].startswith("4")  # full st_mode: directory type bits
    a = entries[1]
    assert a["mode"].startswith("100")  # regular-file type bits
    assert a["size"] == 5
    assert isinstance(a["mtime_ns"], int)
    link = entries[2]
    assert link["link"] == "a.txt"
    assert "size" not in link


def test_newline_filename_roundtrips(ws):
    # v2's line-oriented manifest had to skip these; JSONL representing the
    # name is exactly why the restriction is gone.
    ws.write("data/good.txt", "good")
    (ws.root / "data" / "a\nb.txt").write_text("newline name")
    ws.config({"data": {"path": str(ws.root / "data")}})

    ws.run("push", "data", expect_rc=0)
    assert "data/a\nb.txt" in ws.keys()

    res = ws.run("status", "data", expect_rc=0)
    assert res.out.strip() == ""

    dest = ws.root / "out"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert (dest / "a\nb.txt").read_text() == "newline name"


def test_filename_with_arrow_quote_roundtrips(ws):
    # A file literally named with ' -> ' must not be confused with a symlink
    # record, so its manifest entry matches the local file after a push.
    ws.write("data/a' -> 'b", "content")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    res = ws.run("status", "data", expect_rc=0)
    assert res.out.strip() == ""
