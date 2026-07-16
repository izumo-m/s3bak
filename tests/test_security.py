"""Restore must never write outside its target from a corrupt/hostile manifest.

The manifest is downloaded from S3 and drives local filesystem writes on pull.
These tests feed crafted manifests through the pull path to pin the restore-root
containment guard: parent-traversal (``..``), absolute paths, and writes through
a symlink an earlier record planted.
"""

from __future__ import annotations

import json
import os

from s3bak import restore

MTIME_NS = 1_600_000_000 * 1_000_000_000


def _line(mode: str, rel: str, *, size: int | None = None, link: str | None = None) -> str:
    obj: dict[str, object] = {"path": rel, "mode": mode, "owner": "o", "group": "g"}
    if size is not None:
        obj["size"] = size
    if link is not None:
        obj["link"] = link
    obj["mtime_ns"] = MTIME_NS
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


def test_meta_only_pull_replaces_empty_directory_root_symlink_safely(ws):
    (ws.root / "empty").mkdir()
    ws.config({"empty": {"path": str(ws.root / "empty")}})
    ws.run("push", "empty", expect_rc=0)

    victim = ws.root / "victim"
    victim.mkdir()
    marker = victim / "untouched"
    marker.write_text("keep")
    dest = ws.root / "out"
    os.symlink(victim, dest)

    ws.run("pull", "--meta-only", "empty", "-o", str(dest), expect_rc=0)

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

    assert restore.remove_extras([(str(extra), False)]) == (1, 0)
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
