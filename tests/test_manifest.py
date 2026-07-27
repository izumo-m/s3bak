"""Manifest v3: JSONL parse/format, the sorted merge-join, and subtree patching."""

from __future__ import annotations

import io
import json
import os

import pytest

from s3bak import localwalk, manifest

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


def test_format_entry_escapes_posix_surrogate_filename_bytes(tmp_path):
    p = tmp_path / "f"
    p.write_text("x")
    line = manifest.format_entry("./bad\udcff", os.lstat(p), None)

    assert "\\udcff" in line
    line.encode("utf-8")  # the JSONL stream itself remains valid UTF-8
    entry = manifest.parse_entry(line)
    assert entry is not None
    assert entry.path == "./bad\udcff"


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


def test_iter_manifest_rejects_damaged_lines(tmp_path):
    p = tmp_path / "m.jsonl"
    p.write_text(
        '{"s3bak_manifest":3}\ngarbage\n{"path":"./a","mode":"100644","size":1,"mtime_ns":5}\n'
    )
    with pytest.raises(manifest.ManifestError, match="line 2"):
        list(manifest.iter_manifest(str(p)))


def test_iter_manifest_rejects_empty_symlink_target(tmp_path):
    # os.symlink("", target) fails mid-restore after _place_symlink already
    # removed the existing file; reject it at download instead.
    p = tmp_path / "m.jsonl"
    p.write_text('{"s3bak_manifest":3}\n{"path":"./ln","mode":"120777","mtime_ns":0,"link":""}\n')
    with pytest.raises(manifest.ManifestError):
        list(manifest.iter_manifest(str(p)))


def test_iter_manifest_rejects_symlink_without_target(tmp_path):
    p = tmp_path / "m.jsonl"
    p.write_text('{"s3bak_manifest":3}\n{"path":"./ln","mode":"120777","mtime_ns":0}\n')
    with pytest.raises(manifest.ManifestError):
        list(manifest.iter_manifest(str(p)))


def test_iter_manifest_rejects_oversized_size(tmp_path):
    # A size past off_t max is a damaged record and would overflow compare.py's
    # float size formatting.
    big = 1 << 63
    p = tmp_path / "m.jsonl"
    p.write_text(
        f'{{"s3bak_manifest":3}}\n{{"path":"./a","mode":"100644","size":{big},"mtime_ns":0}}\n'
    )
    with pytest.raises(manifest.ManifestError):
        list(manifest.iter_manifest(str(p)))


def test_iter_manifest_rejects_surrogate_owner(tmp_path):
    # A lone surrogate in owner/group is not UTF-8-encodable and crashes
    # ls-remote's stdout write; reject the record at parse time.
    p = tmp_path / "m.jsonl"
    p.write_text(
        '{"s3bak_manifest":3}\n'
        '{"path":".","mode":"40755","owner":"\\ud800","group":"g","mtime_ns":0}\n'
    )
    with pytest.raises(manifest.ManifestError):
        list(manifest.iter_manifest(str(p)))


def test_iter_manifest_rejects_deeply_nested_json(tmp_path):
    # A deeply-nested value makes json.loads raise RecursionError; it must
    # become a ManifestError, not an uncaught traceback.
    deep = "[" * 100000 + "]" * 100000
    p = tmp_path / "m.jsonl"
    p.write_text(
        '{"s3bak_manifest":3}\n'
        '{"path":"./a","mode":"100644","size":1,"mtime_ns":0,"x":' + deep + "}\n"
    )
    with pytest.raises(manifest.ManifestError):
        list(manifest.iter_manifest(str(p)))


def test_iter_manifest_rejects_overlong_version_integer(tmp_path):
    p = tmp_path / "m.jsonl"
    p.write_text('{"s3bak_manifest":' + "1" * 5000 + "}\n")
    with pytest.raises(manifest.ManifestError):
        list(manifest.iter_manifest(str(p)))


def test_validate_manifest_rejects_header_only_file(tmp_path):
    p = tmp_path / "m.jsonl"
    p.write_text('{"s3bak_manifest":3}\n')
    with pytest.raises(manifest.ManifestError, match="no records"):
        manifest.validate_manifest(str(p))


@pytest.mark.parametrize(
    "records",
    [
        ['{"path":"./missing/child","mode":"100644","size":1,"mtime_ns":0}'],
        [
            '{"path":"./link","mode":"120777","mtime_ns":0,"link":"target"}',
            '{"path":"./link/child","mode":"100644","size":1,"mtime_ns":0}',
        ],
    ],
)
def test_validate_manifest_requires_recorded_directory_parents(tmp_path, records):
    p = tmp_path / "m.jsonl"
    p.write_text(
        "\n".join(
            [
                '{"s3bak_manifest":3}',
                '{"path":".","mode":"40755","mtime_ns":0}',
                *records,
                "",
            ]
        )
    )

    with pytest.raises(manifest.ManifestError, match="directory parent"):
        manifest.validate_manifest(str(p))


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


# --- pattern matching / sort key ------------------------------------------------


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


def test_path_match_invalid_range_never_raises():
    assert not manifest.path_match("a", "[z-a]")


def test_entry_sort_key():
    assert manifest.entry_sort_key(".", True) == ""  # root always first
    assert manifest.entry_sort_key("./foo", True) == "foo/"
    assert manifest.entry_sort_key("./foo.txt", False) == "foo.txt"
    assert manifest.entry_sort_key("solo.txt", False) == "solo.txt"


# --- merge_join -----------------------------------------------------------------


def test_merge_join_pairs_and_one_sided_keys_in_order():
    a = [("a", 1), ("c", 3), ("d", 4)]
    b = [("b", "B"), ("c", "C"), ("e", "E")]
    assert list(manifest.merge_join(a, b)) == [
        ("a", 1, None),
        ("b", None, "B"),
        ("c", 3, "C"),
        ("d", 4, None),
        ("e", None, "E"),
    ]


def test_merge_join_empty_sides():
    assert list(manifest.merge_join([], [])) == []
    assert list(manifest.merge_join([("k", 1)], [])) == [("k", 1, None)]
    assert list(manifest.merge_join([], [("k", 2)])) == [("k", None, 2)]


def test_merge_join_is_lazy():
    # One-record lookahead per side: the join must not drain its inputs ahead
    # of the consumer (that laziness is what makes status constant-memory).
    def infinite():
        n = 0
        while True:
            yield (f"{n:09d}", n)
            n += 1

    it = manifest.merge_join(infinite(), infinite())
    key, left, right = next(it)
    assert (key, left, right) == ("000000000", 0, 0)


# --- subtree patch -------------------------------------------------------------


def _manifest_text(lines: list[str]) -> str:
    return "\n".join(['{"s3bak_manifest":3}', *lines]) + "\n"


def test_write_merged_replaces_subtree_in_order(tmp_path):
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
    manifest.write_merged(out, str(old), "sub", localwalk.iter_subtree(str(local_sub), "sub", []))
    lines = out.getvalue().splitlines()
    assert json.loads(lines[0]) == {"s3bak_manifest": 3}
    rels = [json.loads(ln)["path"] for ln in lines[1:]]
    assert rels == [".", "./a.txt", "./sub.txt", "./sub", "./sub/new.txt", "./z.txt"]


def test_write_merged_removes_deleted_subtree(tmp_path):
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
    manifest.write_merged(out, str(old), "gone", [])
    rels = [json.loads(ln)["path"] for ln in out.getvalue().splitlines()[1:]]
    assert rels == [".", "./keep.txt"]


def test_write_merged_whole_entry_mirror_drops_old_only_records(tmp_path):
    # sub=None makes the whole tree the replaced range: with keep_old=False the
    # output is exactly the fresh walk, regardless of what the old manifest held.
    old = tmp_path / "old.jsonl"
    old.write_text(
        _manifest_text(
            [
                '{"path":".","mode":"40755","mtime_ns":0}',
                '{"path":"./gone.txt","mode":"100644","size":1,"mtime_ns":0}',
                '{"path":"./keep.txt","mode":"100644","size":1,"mtime_ns":0}',
            ]
        )
    )
    root = tmp_path / "root"
    root.mkdir()
    (root / "keep.txt").write_text("k")

    out = io.StringIO()
    manifest.write_merged(out, str(old), None, localwalk.walk_tree(str(root), []))
    rels = [json.loads(ln)["path"] for ln in out.getvalue().splitlines()[1:]]
    assert rels == [".", "./keep.txt"]


def test_write_merged_keep_all_retains_old_only_records_verbatim(tmp_path):
    # keep_old=True: locally-vanished files, symlinks, and empty dirs all keep
    # their records, copied verbatim (unknown JSON keys survive). A path present
    # on both sides takes the fresh walk record.
    old = tmp_path / "old.jsonl"
    old.write_text(
        _manifest_text(
            [
                '{"path":".","mode":"40755","mtime_ns":0}',
                '{"path":"./emptydir","mode":"40755","mtime_ns":0,"future":"kept"}',
                '{"path":"./gone.txt","mode":"100644","size":1,"mtime_ns":0}',
                '{"path":"./keep.txt","mode":"100644","size":1,"mtime_ns":7}',
                '{"path":"./link","mode":"120777","mtime_ns":0,"link":"gone.txt"}',
            ]
        )
    )
    root = tmp_path / "root"
    root.mkdir()
    (root / "keep.txt").write_text("changed")

    out = io.StringIO()
    manifest.write_merged(out, str(old), None, localwalk.walk_tree(str(root), []), keep_old=True)
    lines = out.getvalue().splitlines()[1:]
    entries = [json.loads(ln) for ln in lines]
    assert [e["path"] for e in entries] == [
        ".",
        "./emptydir",
        "./gone.txt",
        "./keep.txt",
        "./link",
    ]
    assert entries[1]["future"] == "kept"  # verbatim copy, unknown key preserved
    assert entries[3]["mtime_ns"] != 7  # both sides: the fresh walk record won


# --- the push journal ----------------------------------------------------------


_OLD_LINES = [
    '{"path":".","mode":"40755","mtime_ns":0}',
    '{"path":"./gone.txt","mode":"100644","size":1,"mtime_ns":0}',
    '{"path":"./keep.txt","mode":"100644","size":1,"mtime_ns":7,"future":"kept"}',
    '{"path":"./link","mode":"120777","mtime_ns":0,"link":"gone.txt"}',
]


def _write_journal(tmp_path, lines: list[str]) -> str:
    p = tmp_path / "push.journal"
    p.write_text("".join(line + "\n" for line in lines))
    return str(p)


def test_merge_journal_applies_events_and_copies_the_rest_verbatim(tmp_path):
    old = tmp_path / "old.jsonl"
    old.write_text(_manifest_text(_OLD_LINES))
    journal = _write_journal(
        tmp_path,
        [
            '+{"path":"./added.txt","mode":"100644","size":2,"mtime_ns":1}',
            "-" + _OLD_LINES[1],  # gone.txt: a confirmed deletion drops its record
            '!{"path":"./link","mode":"120777","mtime_ns":0,"link":"added.txt"}',
        ],
    )
    out_path = tmp_path / "merged.jsonl"
    with open(out_path, "w", encoding="utf-8") as out:
        manifest.merge_journal(out, str(old), journal)
    entries = [json.loads(ln) for ln in out_path.read_text().splitlines()[1:]]
    assert [e["path"] for e in entries] == [".", "./added.txt", "./keep.txt", "./link"]
    assert entries[2]["future"] == "kept"  # untouched record copied verbatim
    assert entries[3]["link"] == "added.txt"  # ! replaced the record
    assert manifest.validate_manifest(str(out_path)) == "dir"


def test_merge_journal_without_old_manifest_is_the_first_push(tmp_path):
    journal = _write_journal(
        tmp_path,
        [
            '+{"path":".","mode":"40755","mtime_ns":0}',
            '+{"path":"./a.txt","mode":"100644","size":1,"mtime_ns":0}',
        ],
    )
    out_path = tmp_path / "merged.jsonl"
    with open(out_path, "w", encoding="utf-8") as out:
        manifest.merge_journal(out, None, journal)
    assert manifest.validate_manifest(str(out_path)) == "dir"


@pytest.mark.parametrize(
    ("event", "message"),
    [
        ('+{"path":"./keep.txt","mode":"100644","size":1,"mtime_ns":7}', "already-recorded"),
        ('!{"path":"./zzz.txt","mode":"100644","size":1,"mtime_ns":0}', "unrecorded"),
        ('-{"path":"./zzz.txt","mode":"100644","size":1,"mtime_ns":0}', "unrecorded"),
        ('-{"path":"./keep.txt","mode":"100644","size":9,"mtime_ns":7}', "does not match"),
    ],
)
def test_merge_journal_marker_mismatch_fails_closed(tmp_path, event, message):
    # A + whose key exists, a ! / - whose key does not, or a - payload that
    # differs from the record it drops is an emitter bug, never absorbed.
    old = tmp_path / "old.jsonl"
    old.write_text(_manifest_text(_OLD_LINES))
    journal = _write_journal(tmp_path, [event])
    with pytest.raises(manifest.ManifestError, match=message):
        manifest.merge_journal(io.StringIO(), str(old), journal)


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        (['*{"path":"./a.txt","mode":"100644","size":1,"mtime_ns":0}'], "invalid journal marker"),
        (["+not json"], "invalid journal record"),
        (
            [
                '+{"path":"./b.txt","mode":"100644","size":1,"mtime_ns":0}',
                '+{"path":"./a.txt","mode":"100644","size":1,"mtime_ns":0}',
            ],
            "out of order",
        ),
        (
            [
                '-{"path":"./a.txt","mode":"100644","size":1,"mtime_ns":0}',
                '+{"path":"./a.txt","mode":"100644","size":2,"mtime_ns":1}',
            ],
            "out of order",  # one key, one event: a -/+ pair must have been a !
        ),
    ],
)
def test_iter_journal_validates_shape(tmp_path, lines, message):
    journal = _write_journal(tmp_path, lines)
    with pytest.raises(manifest.ManifestError, match=message):
        list(manifest.iter_journal(journal))


def test_merge_journal_warns_when_records_survive_under_a_file(tmp_path):
    # A + at the free file key "d" while the old dir record and its children
    # survive: same restorability warning as write_merged, once per subtree.
    old = tmp_path / "old.jsonl"
    old.write_text(
        _manifest_text(
            [
                '{"path":".","mode":"40755","mtime_ns":0}',
                '{"path":"./d","mode":"40755","mtime_ns":0}',
                '{"path":"./d/x.txt","mode":"100644","size":1,"mtime_ns":0}',
            ]
        )
    )
    journal = _write_journal(tmp_path, ['+{"path":"./d","mode":"100644","size":1,"mtime_ns":0}'])
    warnings: list[str] = []
    manifest.merge_journal(io.StringIO(), str(old), journal, warn=warnings.append)
    assert len(warnings) == 1
    assert "./d" in warnings[0]


def test_write_merged_warns_once_when_records_survive_under_a_file(tmp_path):
    # The local dir `d` became a regular file while its old records are kept:
    # the manifest is no longer restorable as a tree. One warning per subtree,
    # even with several surviving descendants, and the sibling "d.txt" (which
    # sorts between the file key `d` and the range `d/`) must not reset it.
    old = tmp_path / "old.jsonl"
    old.write_text(
        _manifest_text(
            [
                '{"path":".","mode":"40755","mtime_ns":0}',
                '{"path":"./d.txt","mode":"100644","size":1,"mtime_ns":0}',
                '{"path":"./d","mode":"40755","mtime_ns":0}',
                '{"path":"./d/x.txt","mode":"100644","size":1,"mtime_ns":0}',
                '{"path":"./d/y.txt","mode":"100644","size":1,"mtime_ns":0}',
            ]
        )
    )
    root = tmp_path / "root"
    root.mkdir()
    (root / "d").write_text("now a file")
    (root / "d.txt").write_text("sibling")

    warnings: list[str] = []
    out = io.StringIO()
    manifest.write_merged(
        out,
        str(old),
        None,
        localwalk.walk_tree(str(root), []),
        keep_old=True,
        warn=warnings.append,
    )
    rels = [json.loads(ln)["path"] for ln in out.getvalue().splitlines()[1:]]
    assert rels == [".", "./d", "./d.txt", "./d", "./d/x.txt", "./d/y.txt"]
    assert len(warnings) == 1
    assert "./d" in warnings[0]


# --- RecordedFiles -------------------------------------------------------------


def test_recorded_files_matches_only_regular_file_records(tmp_path):
    # Only regular files own S3 objects: dir and symlink records never match,
    # and the ascending one-record cursor skips over them.
    path = tmp_path / "m.jsonl"
    path.write_text(
        _manifest_text(
            [
                '{"path":".","mode":"40755","mtime_ns":0}',
                '{"path":"./a.txt","mode":"100644","size":1,"mtime_ns":0}',
                '{"path":"./link","mode":"120777","mtime_ns":0,"link":"a.txt"}',
                '{"path":"./sub","mode":"40755","mtime_ns":0}',
                '{"path":"./sub/b.txt","mode":"100644","size":1,"mtime_ns":0}',
            ]
        )
    )
    files = manifest.RecordedFiles(str(path))
    try:
        assert files.contains("a.txt") is True
        assert files.contains("link") is False
        assert files.contains("other.txt") is False
        assert files.contains("sub/b.txt") is True
        assert files.contains("zz") is False
    finally:
        files.close()


def test_recorded_files_none_path_records_nothing():
    files = manifest.RecordedFiles(None)
    try:
        assert files.contains("anything") is False
    finally:
        files.close()


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
