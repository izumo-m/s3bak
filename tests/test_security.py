"""Restore must never write outside its target from a corrupt/hostile manifest.

The manifest is downloaded from S3 and drives local filesystem writes on pull.
These tests feed crafted manifests through the pull path to pin the restore-root
containment guard: parent-traversal (``..``), absolute paths, and writes through
a symlink an earlier record planted.
"""

from __future__ import annotations

import json

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


def test_pull_rejects_write_through_planted_symlink(ws):
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
    assert (dest / "link").is_symlink()  # the link record itself still restores
