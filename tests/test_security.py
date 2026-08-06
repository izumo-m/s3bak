"""Restore must never write outside its target from a corrupt/hostile manifest.

The manifest is downloaded from S3 and drives local filesystem writes on pull.
These tests feed crafted manifests through the pull path to pin the restore-root
containment guard: parent-traversal (``..``), absolute paths, and writes through
a symlink an earlier record planted.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from s3bak import restore

MTIME_NS = 1_600_000_000 * 1_000_000_000


def _line(
    mode: str,
    rel: str,
    *,
    size: int | None = None,
    link: str | None = None,
    mtime_ns: int = MTIME_NS,
) -> str:
    obj: dict[str, object] = {"path": rel, "mode": mode, "owner": "o", "group": "g"}
    if size is not None:
        obj["size"] = size
    if link is not None:
        obj["link"] = link
    obj["mtime_ns"] = mtime_ns
    return json.dumps(obj)


def _put_manifest(ws, entry: str, lines: list[str]) -> None:
    body = ("\n".join(['{"s3bak_manifest":3}', *lines]) + "\n").encode()
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/{entry}-manifest.jsonl", Body=body)


def test_pull_rejects_parent_traversal_path(ws):
    dest = ws.root / "out"
    escape = ws.root / "escape.txt"  # a sibling of dest, OUTSIDE the restore root
    _put_manifest(ws, "data", [_line("40755", "."), _line("100644", "./../escape.txt", size=4)])
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("pull", "data", "-o", str(dest))
    assert res.rc != 0
    assert "escapes restore root" in (res.err + res.out).lower()
    assert not escape.exists()  # never written outside the restore root


def test_pull_rejects_absolute_path(ws):
    dest = ws.root / "out"
    pwned = ws.root / "pwned"  # absolute, outside dest
    _put_manifest(ws, "data", [_line("40755", "."), _line("40755", str(pwned))])
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("pull", "data", "-o", str(dest))
    assert res.rc != 0
    assert not pwned.exists()


def test_pull_rejects_manifest_descendant_below_symlink(ws):
    victim = ws.root / "victim"
    victim.mkdir()
    dest = ws.root / "out"
    _put_manifest(
        ws,
        "data",
        [
            _line("40755", "."),
            _line("120777", "./link", link=str(victim)),
            _line("40755", "./link/sub"),  # would land in victim/ if the link were followed
        ],
    )
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("pull", "data", "-o", str(dest))
    assert res.rc != 0
    assert not (victim / "sub").exists()  # the planted symlink was not written through
    assert not (dest / "link").exists()  # fail closed before applying any record


def test_pull_meta_only_does_not_apply_file_metadata_through_symlink(ws):
    source = ws.write("data/a.txt", "backup")
    os.chmod(source, 0o600)
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    victim = ws.write("victim.txt", "do not touch")
    os.chmod(victim, 0o644)
    before = os.lstat(victim)
    dest = ws.root / "out"
    dest.mkdir()
    os.symlink(victim, dest / "a.txt")

    res = ws.run("pull", "--meta-only", "data", "-o", str(dest))

    assert res.rc == 1
    after = os.lstat(victim)
    assert (after.st_mode, after.st_mtime_ns) == (before.st_mode, before.st_mtime_ns)
    assert (dest / "a.txt").is_symlink()


def test_push_subpath_rejects_symlinked_ancestor(ws):
    # `push entry/link/passwd` with `link` a symlink to an outside directory
    # would make os.lstat(target_root/link/passwd) resolve to the outside file
    # and upload it as entry/link/passwd - data outside the entry, left as an
    # unrecorded object (the ancestor cannot form a valid directory record).
    # The push must refuse before any upload.
    ws.write("data/a.txt", "x")
    secret_dir = ws.root / "secret"
    secret_dir.mkdir()
    (secret_dir / "passwd").write_text("root:x:0:0")
    os.symlink(secret_dir, ws.root / "data" / "link")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("push", "data/link/passwd")
    assert res.rc == 1
    assert "ancestor" in res.err.lower()
    assert "data/link/passwd" not in ws.keys()
    assert not any(k.endswith("link/passwd") for k in ws.keys())


def test_pull_subpath_rejects_symlinked_ancestor(ws):
    # A local `dir -> /outside` ancestor would make `pull entry/dir/file` write
    # its download to /outside/file (get_object creates its temp file in
    # dirname(dest), which resolves through the link). Refuse before any write.
    ws.write("data/dir/file.txt", "content")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    outside = ws.root / "outside"
    outside.mkdir()
    os.remove(ws.root / "data" / "dir" / "file.txt")
    os.rmdir(ws.root / "data" / "dir")
    os.symlink(outside, ws.root / "data" / "dir")

    res = ws.run("pull", "data/dir/file.txt")
    assert res.rc == 1
    assert "ancestor" in res.err.lower()
    assert not (outside / "file.txt").exists()  # nothing written outside the entry


def test_pull_subpath_rejects_symlinked_entry_root(ws):
    # The entry root itself being a symlink (not just an intermediate ancestor)
    # would make `pull entry/file` write through it, outside the entry.
    ws.write("data/file.txt", "content")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    outside = ws.root / "outside"
    outside.mkdir()
    os.remove(ws.root / "data" / "file.txt")
    os.rmdir(ws.root / "data")
    os.symlink(outside, ws.root / "data")  # relocate the whole entry root

    res = ws.run("pull", "data/file.txt")
    assert res.rc == 1
    assert "ancestor" in res.err.lower()
    assert not (outside / "file.txt").exists()


def test_pull_rejects_manifest_with_nul_in_symlink_target(ws):
    # A NUL in a symlink target survives every type check but would raise
    # ValueError in os.symlink/os.path.isdir on restore, bypassing run()'s
    # operational-error handling. It must be rejected at download.
    _put_manifest(ws, "data", [_line("40755", "."), _line("120777", "./bad", link="x\x00y")])
    ws.config({"data": {"path": str(ws.root / "data")}})
    res = ws.run("pull", "data", "-o", str(ws.root / "out"))
    assert res.rc == 1
    assert "invalid manifest" in res.err.lower()
    assert not (ws.root / "out").exists()  # failed closed before any write


def test_verify_checksum_skips_file_under_symlinked_ancestor(ws):
    # verify --checksum must not hash a file reached through a local symlinked
    # ancestor: doing so would read an entry-outside file and wrongly flag the
    # healthy backup as "content differs but size+mtime match".
    src = ws.write("data/dir/f.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    recorded = os.lstat(src)

    outside = ws.root / "outside"
    outside.mkdir()
    decoy = outside / "f.txt"
    decoy.write_text("world")  # same size (5), different content
    os.utime(decoy, ns=(recorded.st_mtime_ns, recorded.st_mtime_ns))  # same mtime
    os.remove(src)
    os.rmdir(ws.root / "data" / "dir")
    os.symlink(outside, ws.root / "data" / "dir")

    res = ws.run("verify", "--checksum", "data")
    assert "content differs" not in res.err  # the symlinked ancestor was skipped


def test_verify_checksum_skips_when_entry_root_is_symlink(ws):
    # The entry root itself being a symlink (not just an intermediate ancestor)
    # must skip the content hash, not read through it.
    src = ws.write("data/f.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    recorded = os.lstat(src)

    outside = ws.root / "outside"
    outside.mkdir()
    decoy = outside / "f.txt"
    decoy.write_text("world")  # same size, different content
    os.utime(decoy, ns=(recorded.st_mtime_ns, recorded.st_mtime_ns))
    shutil.rmtree(ws.root / "data")
    os.symlink(outside, ws.root / "data")  # entry root is now a symlink

    res = ws.run("verify", "--checksum", "data")
    assert "content differs" not in res.err


def test_verify_checksum_file_subpath_skips_symlinked_ancestor(ws):
    # The explicit file sub-path verify goes through _verify_file_record, which
    # must also skip the content hash under a symlinked ancestor.
    src = ws.write("data/dir/f.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    recorded = os.lstat(src)

    outside = ws.root / "outside"
    outside.mkdir()
    decoy = outside / "f.txt"
    decoy.write_text("world")
    os.utime(decoy, ns=(recorded.st_mtime_ns, recorded.st_mtime_ns))
    shutil.rmtree(ws.root / "data" / "dir")
    os.symlink(outside, ws.root / "data" / "dir")

    res = ws.run("verify", "--checksum", "data/dir/f.txt")
    assert "content differs" not in res.err


def test_diff_file_subpath_does_not_follow_symlinked_ancestor(ws):
    # An explicit file sub-path diff must not read an entry-outside file through
    # a symlinked ancestor and disclose its contents.
    ws.write("data/dir/f.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    outside = ws.root / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("SECRET OUTSIDE CONTENT")
    shutil.rmtree(ws.root / "data" / "dir")
    os.symlink(outside, ws.root / "data" / "dir")

    res = ws.run("diff", "data/dir/f.txt")
    assert "SECRET OUTSIDE CONTENT" not in res.out
    assert "unreachable" in (res.out + res.err).lower()


def test_status_leaf_subpath_through_symlinked_ancestor_shows_missing(ws):
    # status entry/d/f with local `d` a symlink to a decoy that matches size,
    # mtime and mode must not read through the symlink and report clean; it must
    # show D, consistent with the full-entry no-follow walk.
    src = ws.write("data/d/f.txt", "hello")
    os.chmod(src, 0o644)
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    recorded = os.lstat(src)

    outside = ws.root / "outside"
    outside.mkdir()
    decoy = outside / "f.txt"
    decoy.write_text("world")  # same size (5)
    os.chmod(decoy, 0o644)
    os.utime(decoy, ns=(recorded.st_mtime_ns, recorded.st_mtime_ns))
    os.remove(src)
    os.rmdir(ws.root / "data" / "d")
    os.symlink(outside, ws.root / "data" / "d")

    res = ws.run("status", "data/d/f.txt", expect_rc=0)
    assert any(ln.startswith("D ") for ln in res.out.splitlines())  # missing, not clean


def test_diff_symlink_leaf_through_symlinked_ancestor_not_clean(ws):
    # diff entry/d/link (a recorded symlink) with local `d` a symlink to a decoy
    # holding the SAME symlink must not falsely exit 0 by reading the decoy.
    (ws.root / "data" / "d").mkdir(parents=True)
    os.symlink("/some/target", ws.root / "data" / "d" / "link")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    outside = ws.root / "outside"
    outside.mkdir()
    os.symlink("/some/target", outside / "link")  # decoy with the same target
    shutil.rmtree(ws.root / "data" / "d")
    os.symlink(outside, ws.root / "data" / "d")

    res = ws.run("diff", "data/d/link")
    assert res.rc == 1  # not falsely clean
    assert "unreachable" in (res.out + res.err).lower()


def test_diff_dir_subpath_does_not_follow_ancestor_above_sub(ws):
    # `diff entry/d/e` with local `d` (an ancestor ABOVE the sub root `d/e`) a
    # symlink to an outside tree must not read the outside content - diff_backup's
    # own guards only cover ancestors at/under the sub root.
    ws.write("data/d/e/f.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    outside = ws.root / "outside"
    (outside / "e").mkdir(parents=True)
    (outside / "e" / "f.txt").write_text("SECRET OUTSIDE")
    shutil.rmtree(ws.root / "data" / "d")
    os.symlink(outside, ws.root / "data" / "d")

    res = ws.run("diff", "data/d/e")
    assert "SECRET OUTSIDE" not in res.out
    assert "unreachable" in (res.out + res.err).lower()


def test_pull_symlink_over_file_preserves_file_when_symlink_creation_fails(ws, monkeypatch):
    # If os.symlink fails while replacing a local regular file with a recorded
    # symlink, the existing file must survive - a failed pull must not cost it.
    _put_manifest(ws, "data", [_line("40755", "."), _line("120777", "./link", link="somewhere")])
    ws.config({"data": {"path": str(ws.root / "data")}})
    dest = ws.root / "out"
    dest.mkdir()
    victim = dest / "link"
    victim.write_text("precious")

    def failing_symlink(src, dst, target_is_directory=False):
        raise PermissionError("no symlink privilege")

    monkeypatch.setattr("s3bak.restore.os.symlink", failing_symlink)
    # cli.main lets the OSError propagate (cli.run maps it to exit 1); the point
    # is that the existing file is NOT destroyed on the way to that failure.
    with pytest.raises(PermissionError):
        ws.run("pull", "data", "-o", str(dest))
    assert victim.exists() and victim.read_text() == "precious"  # existing file intact


def test_validate_rejects_backslash_path_component_on_windows(ws, monkeypatch):
    # A backslash is a legal POSIX filename character but a Windows path
    # separator; a record like "./a\\b" restores as a nested dir/file on Windows.
    # Validation must accept it on POSIX and fail closed on Windows.
    from s3bak import manifest

    _put_manifest(ws, "data", [_line("40755", "."), _line("100644", "./a\\b", size=1)])
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a\\b", Body=b"x")
    ws.config({"data": {"path": str(ws.root / "data")}})

    ws.run("pull", "data", "-o", str(ws.root / "out_posix"), expect_rc=0)  # legal on POSIX

    monkeypatch.setattr(manifest.os, "name", "nt")  # simulate Windows
    res = ws.run("pull", "data", "-o", str(ws.root / "out_win"))
    assert res.rc == 1
    assert "backslash" in res.err.lower()
    assert not (ws.root / "out_win").exists()  # failed closed before any transfer


def test_validate_rejects_drive_qualified_path_component_on_windows(ws, monkeypatch):
    # "C:" is a legal POSIX filename component but a Windows drive spec; a
    # record like "./C:/win.ini" makes os.path.join drop the restore root
    # entirely and land at the drive-qualified path instead (the same shape
    # cli.py's sub-path resolution already rejects on CLI input). Validation
    # must accept it on POSIX and fail closed on Windows.
    import ntpath

    from s3bak import manifest

    _put_manifest(
        ws,
        "data",
        [_line("40755", "."), _line("40755", "./C:"), _line("100644", "./C:/win.ini", size=1)],
    )
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/C:/win.ini", Body=b"x")
    ws.config({"data": {"path": str(ws.root / "data")}})

    ws.run("pull", "data", "-o", str(ws.root / "out_posix"), expect_rc=0)  # legal on POSIX

    monkeypatch.setattr(manifest.os, "name", "nt")  # simulate Windows
    # os.path is resolved to posixpath at interpreter startup on this (real
    # POSIX) test host, so os.name alone does not make os.path.splitdrive
    # behave like Windows; swap in ntpath's (platform-independent, pure
    # string logic) implementation for the duration of this simulation.
    monkeypatch.setattr(manifest.os.path, "splitdrive", ntpath.splitdrive)
    res = ws.run("pull", "data", "-o", str(ws.root / "out_win"))
    assert res.rc == 1
    assert "drive" in res.err.lower()
    assert not (ws.root / "out_win").exists()  # failed closed before any transfer


def test_pull_handles_unrepresentable_mtime_ns(ws):
    # A damaged manifest with an out-of-range mtime_ns must fail cleanly (exit 1),
    # not crash pull with an uncaught OverflowError from os.utime.
    body = (
        '{"s3bak_manifest":3}\n'
        + json.dumps(
            {"path": ".", "mode": "40755", "owner": "o", "group": "g", "mtime_ns": MTIME_NS}
        )
        + "\n"
        + json.dumps(
            {
                "path": "./f.txt",
                "mode": "100644",
                "owner": "o",
                "group": "g",
                "size": 3,
                "mtime_ns": 10**30,
            }
        )
        + "\n"
    ).encode()
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl", Body=body)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/f.txt", Body=b"abc")
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("pull", "data", "-o", str(ws.root / "out"))
    assert res.rc == 1
    assert "utime failed" in res.err.lower()


def _conflict_manifest_lines() -> list[str]:
    # A symlink, a directory, AND a file recorded at/under the same path ./d -
    # what "replace a dir with a symlink, then push without --delete" produces.
    return [
        _line("40755", "."),
        _line("120777", "./d", link="/tmp/somewhere"),
        _line("40755", "./d"),
        _line("100644", "./d/x", size=3),
    ]


def test_pull_fails_closed_on_unrestorable_conflict_manifest(ws):
    # Pulling a manifest that records a non-directory and a directory/descendants
    # at one path would restore ./d/x and then rmtree ./d (the deferred symlink
    # replacement), destroying the restored file AND unrecorded local data, before
    # failing. Pull must instead fail closed BEFORE any mutation.
    _put_manifest(ws, "data", _conflict_manifest_lines())
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/d/x", Body=b"abc")
    ws.config({"data": {"path": str(ws.root / "data")}})

    dest = ws.root / "out"
    (dest / "d").mkdir(parents=True)
    precious = dest / "d" / "precious.txt"
    precious.write_text("do not delete me")  # unrecorded local data under d

    res = ws.run("pull", "data", "-o", str(dest))
    assert res.rc == 1
    assert "unrestorable" in res.err.lower()
    assert precious.exists() and precious.read_text() == "do not delete me"  # untouched


def test_verify_reports_unrestorable_conflict_manifest(ws):
    _put_manifest(ws, "data", _conflict_manifest_lines())
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/d/x", Body=b"abc")
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("verify", "data")
    assert res.rc == 1
    assert "unrestorable" in res.err.lower()


def test_subpath_pull_fails_closed_on_root_level_conflict(ws):
    # The conflict is AT the sub root itself (symlink ./d + dir ./d): the
    # sub-scoped check must find it in entry-relative space, not collapse both
    # sub-root records to "." and miss it, letting the destructive restore run.
    _put_manifest(ws, "data", _conflict_manifest_lines())
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/d/x", Body=b"abc")
    ws.config({"data": {"path": str(ws.root / "data")}})

    d = ws.root / "data" / "d"
    d.mkdir(parents=True)
    precious = d / "precious.txt"
    precious.write_text("do not delete me")  # unrecorded local data under the sub

    res = ws.run("pull", "data/d")
    assert res.rc == 1
    assert "unrestorable" in res.err.lower()
    assert precious.exists() and precious.read_text() == "do not delete me"


def test_subpath_verify_reports_root_level_conflict(ws):
    _put_manifest(ws, "data", _conflict_manifest_lines())
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/d/x", Body=b"abc")
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("verify", "data/d")
    assert res.rc == 1
    assert "unrestorable" in res.err.lower()


def test_pull_rejects_manifest_with_unencodable_symlink_target(ws):
    # A lone surrogate in the link target passes every type check but raises
    # UnicodeEncodeError in os.symlink on restore - AFTER _place_symlink removed
    # the existing file (data loss + traceback). Reject it at download instead.
    body = (
        '{"s3bak_manifest":3}\n'
        + json.dumps({"path": ".", "mode": "40755", "owner": "o", "group": "g", "mtime_ns": 0})
        + "\n"
        + json.dumps(
            {
                "path": "./ln",
                "mode": "120777",
                "owner": "o",
                "group": "g",
                "mtime_ns": 0,
                "link": "\ud800",
            }
        )
        + "\n"
    ).encode()
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl", Body=body)
    ws.config({"data": {"path": str(ws.root / "data")}})

    dest = ws.root / "out"
    dest.mkdir()
    victim = dest / "ln"
    victim.write_text("do not delete me")

    res = ws.run("pull", "data", "-o", str(dest))
    assert res.rc == 1
    assert "invalid manifest" in res.err.lower()
    assert victim.exists() and victim.read_text() == "do not delete me"  # untouched


def test_pull_replaces_symlink_directory_root_without_writing_through_it(ws):
    ws.write("data/a.txt", "backup")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    victim = ws.root / "victim"
    victim.mkdir()
    dest = ws.root / "out"
    os.symlink(victim, dest)

    ws.run("pull", "data", "-o", str(dest), expect_rc=0)

    assert not dest.is_symlink()
    assert (dest / "a.txt").read_text() == "backup"
    assert not (victim / "a.txt").exists()


def test_pull_replaces_empty_directory_root_symlink_safely(ws):
    (ws.root / "empty").mkdir()
    ws.config({"empty": {"path": str(ws.root / "empty")}})
    ws.run("push", "empty", expect_rc=0)

    victim = ws.root / "victim"
    victim.mkdir()
    marker = victim / "untouched"
    marker.write_text("keep")
    dest = ws.root / "out"
    os.symlink(victim, dest)

    ws.run("pull", "empty", "-o", str(dest), expect_rc=0)

    assert dest.is_dir() and not dest.is_symlink()
    assert marker.read_text() == "keep"


def test_corrupt_manifest_fails_before_pull_delete(ws):
    dest = ws.root / "out"
    dest.mkdir()
    extra = dest / "must-survive.txt"
    extra.write_text("local")
    body = b'{"s3bak_manifest":3}\n{"path":".","mode":"40755","mtime_ns":0}\ngarbage\n'
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-manifest.jsonl", Body=body)
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("pull", "data", "-o", str(dest), "--delete")

    assert res.rc == 1
    assert extra.read_text() == "local"


def test_remove_extras_reports_deletion_failure(tmp_path, monkeypatch, capfd):
    extra = tmp_path / "extra.txt"
    extra.write_text("keep")

    def fail_remove(_path):
        raise PermissionError("denied")

    monkeypatch.setattr(restore.os, "remove", fail_remove)

    item = ("extra.txt", str(extra), False)
    assert restore.remove_extras(iter([item]), aliases=set()) == (1, 0)
    assert extra.exists()
    assert "delete failed" in capfd.readouterr().err


def test_pull_replaces_inner_symlink_directory_without_writing_through_it(ws):
    # A pre-existing local symlink where the manifest records a DIRECTORY
    # would route the sync's downloads outside the restore tree; the pull
    # replaces it with a real directory before any bytes move.
    ws.write("data/sub/f.txt", "backup")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    victim = ws.root / "victim"
    victim.mkdir()
    dest = ws.root / "out"
    dest.mkdir()
    os.symlink(victim, dest / "sub")

    ws.run("pull", "data", "-o", str(dest), expect_rc=0)

    assert not (dest / "sub").is_symlink()
    assert (dest / "sub" / "f.txt").read_text() == "backup"
    assert not (victim / "f.txt").exists()


SUB_MTIME_NS = 1_650_000_000 * 1_000_000_000


def test_pull_resettles_directory_mtime_after_symlink_replaces_nested_dir(ws):
    # apply_manifest's ancestor stack pops (and would settle) a directory's
    # frame as soon as the stream proves it has left that subtree - here,
    # right after "./sub/link" is seen, because the later sibling "./zzz"
    # is not one of "./sub"'s descendants. But "./sub/link" is a symlink
    # replacing a local DIRECTORY, so its placement (move-aside + rmtree) is
    # deferred until the whole manifest stream is consumed - well after
    # "./sub" would already have been popped and settled. That placement
    # dirties "./sub"'s own mtime again; without the resettle flag this test
    # pins, that drift would survive the pull.
    _put_manifest(
        ws,
        "data",
        [
            _line("40755", "."),
            _line("40755", "./sub", mtime_ns=SUB_MTIME_NS),
            _line("120777", "./sub/link", link="../outside.txt"),
            _line("40755", "./zzz"),
        ],
    )
    ws.config({"data": {"path": str(ws.root / "data")}})

    dest = ws.root / "out"
    stale = dest / "sub" / "link" / "stale.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale")

    ws.run("pull", "data", "-o", str(dest), expect_rc=0)

    assert (dest / "sub" / "link").is_symlink()
    assert not stale.exists()
    assert os.lstat(dest / "sub").st_mtime_ns == SUB_MTIME_NS


# --- filesystem-alias defenses (W-F3 / W-F4) --------------------------------
#
# A name-folding filesystem (Windows/macOS case-insensitive, Win32's trailing
# dot/space trim, macOS NFC/NFD) can make one physical file answer to two
# byte-different spellings. W-F4 is the destination-overlap guard missing one
# such fold; W-F3 is pull --delete treating the resulting manifest/local
# spelling split as an extra it just restored. Linux cannot host a real
# folding filesystem, so these tests pin restore.fs_alias_key itself (the
# folding rule) and drive restore.remove_extras directly with a hand-built
# ``aliases`` set shaped like what commands._collect_extra_aliases's
# preliminary pass would produce for such a split.


def test_fs_alias_key_folds_case_dot_space_and_unicode_normalization():
    # Case-insensitive filesystems (Windows, default macOS).
    assert restore.fs_alias_key("Report.txt") == restore.fs_alias_key("report.txt")
    # Win32 drops a trailing dot/space from a path's final component.
    assert restore.fs_alias_key("report.") == restore.fs_alias_key("report")
    assert restore.fs_alias_key("name ") == restore.fs_alias_key("name")
    assert restore.fs_alias_key("name. . ") == restore.fs_alias_key("name")
    # macOS normalizes filenames to NFD; a manifest written on a NFC-producing
    # platform (most of POSIX) must still fold onto the NFD spelling.
    nfc = "café"  # "é" as one code point
    nfd = "café"  # "e" + combining acute accent
    assert restore.fs_alias_key(nfc) == restore.fs_alias_key(nfd)


def test_fs_alias_key_does_not_collide_unrelated_names():
    assert restore.fs_alias_key("report.txt") != restore.fs_alias_key("reports.txt")
    assert restore.fs_alias_key("a") != restore.fs_alias_key("b")


def test_canonical_restore_comparison_path_folds_trailing_dot_on_final_component(tmp_path):
    a = str(tmp_path / "data")
    b = str(tmp_path / "data.")
    assert restore.canonical_restore_comparison_path(
        a
    ) == restore.canonical_restore_comparison_path(b)


def test_canonical_restore_comparison_path_folds_dot_on_an_intermediate_component(tmp_path):
    # W-F4: the guard must not only special-case the LAST component - a
    # Win32 path creation drops a trailing dot/space from every component it
    # creates, not only the final one.
    a = str(tmp_path / "data" / "sub")
    b = str(tmp_path / "data." / "sub")
    assert restore.canonical_restore_comparison_path(
        a
    ) == restore.canonical_restore_comparison_path(b)


def test_canonical_restore_comparison_path_distinguishes_unrelated_paths(tmp_path):
    a = str(tmp_path / "data")
    b = str(tmp_path / "other")
    assert restore.canonical_restore_comparison_path(
        a
    ) != restore.canonical_restore_comparison_path(b)


def test_remove_extras_keeps_local_extra_whose_manifest_alias_would_sort_after_it(tmp_path, capfd):
    # W-F3, "report." vs "report": a short name sorts before one it
    # prefixes, so a manifest-only "report." record would sort AFTER the
    # local-only "report" extra in raw merge-join order. commands
    # ._collect_extra_aliases collects the alias set in its own preliminary
    # pass, complete before remove_extras ever runs - so which side the
    # manifest-only partner would have sorted on makes no difference here,
    # unlike the deferred pop-time decision this replaced.
    report = tmp_path / "a" / "report"
    report.parent.mkdir()
    report.write_text("restored content")

    items = [("a/report", str(report), False)]
    aliases = {("a", restore.fs_alias_key("report."))}
    errors, removed = restore.remove_extras(iter(items), aliases=aliases)

    assert (errors, removed) == (0, 0)
    assert report.exists()
    err = capfd.readouterr().err
    assert "warning" in err.lower()
    assert "not removed" in err.lower()


def test_remove_extras_keeps_local_extra_whose_manifest_alias_would_sort_before_it(tmp_path, capfd):
    # W-F3, "B.txt" vs "b.txt": uppercase sorts before lowercase in byte
    # order, so a manifest-only "B.txt" record would sort BEFORE the
    # local-only "b.txt" extra - the other direction from the test above,
    # protected the same way by the same pre-collected set.
    b = tmp_path / "a" / "b.txt"
    b.parent.mkdir()
    b.write_text("restored content")

    items = [("a/b.txt", str(b), False)]
    aliases = {("a", restore.fs_alias_key("B.txt"))}
    errors, removed = restore.remove_extras(iter(items), aliases=aliases)

    assert (errors, removed) == (0, 0)
    assert b.exists()
    err = capfd.readouterr().err
    assert "warning" in err.lower()
    assert "not removed" in err.lower()


def test_remove_extras_keeps_an_aliased_extra_directory_too(tmp_path, capfd):
    # The alias check applies to a directory extra just as much as a leaf -
    # checked at the directory's own pop, against ITS OWN (parent, basename),
    # never its children's.
    d = tmp_path / "a" / "b"
    d.mkdir(parents=True)

    items = [("a/b", str(d), True)]
    aliases = {("a", restore.fs_alias_key("B"))}
    errors, removed = restore.remove_extras(iter(items), aliases=aliases)

    assert (errors, removed) == (0, 0)
    assert d.exists()
    err = capfd.readouterr().err
    assert "warning" in err.lower()
    assert "not removed" in err.lower()


def test_remove_extras_still_removes_an_unaliased_extra_next_to_an_alias_entry(tmp_path, capfd):
    # An alias entry for one name in a directory must not make the check
    # over-conservative: an unrelated local-only name in the same directory,
    # not itself in the alias set, is removed exactly as before.
    extra = tmp_path / "a" / "unrelated.txt"
    extra.parent.mkdir()
    extra.write_text("delete me")

    items = [("a/unrelated.txt", str(extra), False)]
    aliases = {("a", restore.fs_alias_key("deleted-elsewhere.txt"))}
    errors, removed = restore.remove_extras(iter(items), aliases=aliases)

    assert (errors, removed) == (0, 1)
    assert not extra.exists()
    assert "not removed" not in capfd.readouterr().err.lower()


def test_remove_extras_regression_nested_post_order_still_removes_plain_extras(tmp_path, capfd):
    # No aliases at all here - a plain regression check that ordinary extras
    # are still removed, deepest-first within each subtree, exactly as
    # before the W-F3 fix.
    (tmp_path / "extradir" / "sub").mkdir(parents=True)
    deep = tmp_path / "extradir" / "sub" / "deep.txt"
    deep.write_text("d")
    zzz = tmp_path / "zzz.txt"
    zzz.write_text("z")

    items = [
        ("extradir", str(tmp_path / "extradir"), True),
        ("extradir/sub", str(tmp_path / "extradir" / "sub"), True),
        ("extradir/sub/deep.txt", str(deep), False),
        ("zzz.txt", str(zzz), False),
    ]
    errors, removed = restore.remove_extras(iter(items), aliases=set())

    assert (errors, removed) == (0, 4)
    out = capfd.readouterr().out
    deletes = [ln.removeprefix("delete: ") for ln in out.splitlines() if ln.startswith("delete: ")]
    assert deletes == [
        str(deep),
        str(tmp_path / "extradir" / "sub"),
        str(tmp_path / "extradir"),
        str(zzz),
    ]


def test_delete_extras_collects_alias_via_a_name_folding_lexists_probe(
    tmp_path, monkeypatch, capfd
):
    # End-to-end through commands._delete_extras (not restore.remove_extras
    # directly): pins that the alias SET ITSELF ties back to a real
    # name-folding filesystem signal, not a hand-built set. On such a
    # filesystem, os.path.lexists(the manifest's OWN recorded spelling)
    # succeeds even though a plain directory listing enumerates the same
    # file under a different local spelling - the fold happens inside the
    # OS's own path resolution. Linux has no such filesystem, so the fold is
    # simulated by monkeypatching os.path.lexists for exactly that spelling;
    # everything else (the walk, the merge-join, remove_extras) runs for
    # real.
    from s3bak import commands
    from s3bak.config import Opts

    dest = tmp_path / "out"
    dest.mkdir()
    content = "restored content"
    (dest / "report.txt").write_text(content)
    manifest_path = tmp_path / "manifest.jsonl"
    manifest_path.write_text(
        "\n".join(
            [
                '{"s3bak_manifest":3}',
                _line("40755", "."),
                _line("100644", "./Report.txt", size=len(content)),
            ]
        )
        + "\n"
    )

    folded = os.path.join(str(dest), "Report.txt")
    real_lexists = os.path.lexists

    def fake_lexists(path):
        return True if path == folded else real_lexists(path)

    monkeypatch.setattr(commands.os.path, "lexists", fake_lexists)

    status, removed = commands._delete_extras(
        str(manifest_path), str(dest), None, [], opts=Opts(yes=True), entry="data"
    )

    assert (status, removed) == (0, 0)
    assert (dest / "report.txt").read_text() == content
    err = capfd.readouterr().err
    assert "warning" in err.lower()
    assert "not removed" in err.lower()
