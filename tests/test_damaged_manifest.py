"""Command-boundary matrix: every command that reads a manifest must fail
closed on a damaged one - rc=1, a clear stderr message, no traceback, and no
side effect on S3 or the local filesystem. docs/overview.md names this
directly: "operations should fail safely when stored state cannot be
trusted."

tests/test_manifest.py already pins the underlying validation rules at the
function level (parse_entry, iter_manifest, validate_manifest). This file
does not re-test those rules; it pins that every command sharing
syncops.download_manifest reacts to a rejection the same way, before any
mutation.

Commands covered: status, diff, verify, ls-remote, pull, push - every cmd_*
that calls download_manifest (commands.py). `show` is excluded: it streams
the data object directly (Boto3S3Store.stream_object_to_stdout) and never
calls download_manifest, so a damaged manifest cannot affect it - pinned
below instead of included in the matrix. `ls-remote` with no entry argument
is also excluded: it only lists top-level manifest names and never downloads
one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

MTIME_NS = 1_700_000_000 * 1_000_000_000
HEADER = '{"s3bak_manifest":3}'


def _rec(path: str, mode: str, *, size: int | None = None) -> str:
    obj: dict[str, object] = {
        "path": path,
        "mode": mode,
        "owner": "o",
        "group": "g",
        "mtime_ns": MTIME_NS,
    }
    if size is not None:
        obj["size"] = size
    return json.dumps(obj)


_ROOT = _rec(".", "40755")
_A_TXT = _rec("./a.txt", "100644", size=1)


def _manifest_bytes(kind: str) -> bytes:
    """One damaged manifest body for `kind`. Every variant otherwise looks
    like a small, real directory manifest for entry "data" - only the one
    dimension named by `kind` breaks a rule read directly off manifest.py."""
    if kind == "bad_header":
        # _check_header: not a dict, or missing the version key.
        lines = ["this is not a header line", _ROOT, _A_TXT]
        return ("\n".join(lines) + "\n").encode()
    if kind == "invalid_json_line":
        # parse_entry: json.loads itself fails.
        lines = [HEADER, _ROOT, "not-json-at-all"]
        return ("\n".join(lines) + "\n").encode()
    if kind == "missing_field":
        # parse_entry: valid JSON, but no "path" key at all (KeyError).
        bad = json.dumps({"mode": "100644", "size": 1, "mtime_ns": MTIME_NS})
        return ("\n".join([HEADER, _ROOT, bad]) + "\n").encode()
    if kind == "bad_type":
        # parse_entry: "mode" must be an octal string; here it is a bare int
        # (int(int, 8) raises TypeError).
        bad = json.dumps({"path": "./a.txt", "mode": 100644, "size": 1, "mtime_ns": MTIME_NS})
        return ("\n".join([HEADER, _ROOT, bad]) + "\n").encode()
    if kind == "out_of_order":
        # validate_manifest's ascending-key invariant: two top-level files
        # swapped ("./z.txt" sorts after "./a.txt", written first here).
        z_txt = _rec("./z.txt", "100644", size=1)
        a_txt = _rec("./a.txt", "100644", size=1)
        return ("\n".join([HEADER, _ROOT, z_txt, a_txt]) + "\n").encode()
    if kind == "no_directory_parent":
        # validate_manifest's directory-stack check: "./sub/child.txt" with
        # no preceding "./sub" directory record.
        child = _rec("./sub/child.txt", "100644", size=1)
        return ("\n".join([HEADER, _ROOT, child]) + "\n").encode()
    if kind == "zero_records":
        # validate_manifest: a header with no records at all.
        return (HEADER + "\n").encode()
    if kind == "truncated":
        # parse_entry: the last record is cut mid-object, as an interrupted
        # upload would leave it - no closing brace, no trailing newline.
        fragment = '{"path":"./z.txt","mode":"100644","siz'
        return ("\n".join([HEADER, _ROOT, _A_TXT]) + "\n" + fragment).encode()
    raise ValueError(f"unknown damage kind: {kind!r}")


DAMAGE_CASES = [
    ("bad_header", "s3bak v3 manifest"),
    ("invalid_json_line", "invalid manifest record"),
    ("missing_field", "invalid manifest record"),
    ("bad_type", "invalid manifest record"),
    ("out_of_order", "out of order"),
    ("no_directory_parent", "directory parent"),
    ("zero_records", "no records"),
    ("truncated", "invalid manifest record"),
]

COMMANDS = ["status", "diff", "verify", "ls-remote", "pull", "push"]


@pytest.fixture
def pushed_ws(ws):
    """A workspace with entry "data" (a small directory tree) already pushed,
    so the manifest key and shape match what every command expects. Tests
    then overwrite the manifest object in place with damaged bytes."""
    ws.write("data/a.txt", "alpha")
    ws.write("data/sub/b.txt", "beta")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    return ws


def _object_snapshot(ws) -> dict[str, str]:
    """key -> ETag for every object under the workspace prefix. Stronger than
    a bare key set (ws.keys()): it also catches an in-place rewrite of an
    existing key's content - the manifest itself, or a data object - that a
    same-keys check would miss."""
    found: dict[str, str] = {}
    paginator = ws.s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=ws.bucket, Prefix=f"{ws.prefix}/"):
        for obj in page.get("Contents", []):
            found[obj["Key"][len(ws.prefix) + 1 :]] = obj["ETag"]
    return found


def _local_snapshot(root: Path) -> dict[str, bytes | None]:
    """rel path -> file bytes (None for a directory) under `root`, recursively
    - the existence set and content of every entry. Catches any local write,
    removal, or content change a failed command must not have made."""
    if not root.exists():
        return {}
    return {
        str(p.relative_to(root)): (None if p.is_dir() else p.read_bytes())
        for p in sorted(root.rglob("*"))
    }


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize(("kind", "expected"), DAMAGE_CASES, ids=[k for k, _ in DAMAGE_CASES])
def test_command_fails_closed_on_damaged_manifest(pushed_ws, command, kind, expected):
    ws = pushed_ws
    key = f"{ws.prefix}/data-manifest.jsonl"
    ws.s3.put_object(Bucket=ws.bucket, Key=key, Body=_manifest_bytes(kind))

    objects_before = _object_snapshot(ws)
    data_before = _local_snapshot(ws.root / "data")

    dest = ws.root / "restored"
    dest_before: dict[str, bytes | None] | None = None
    if command == "pull":
        # A pre-existing destination with its own content: proves pull did
        # not touch it, not merely that it failed to create one.
        dest.mkdir()
        (dest / "must-survive.txt").write_text("do not touch")
        dest_before = _local_snapshot(dest)
        res = ws.run("pull", "data", "-o", str(dest))
    elif command == "push":
        # --delete --yes: the flag combination most likely to touch S3 or the
        # local tree, so it is the most consequential case to get wrong.
        res = ws.run("push", "--delete", "--yes", "data")
    else:
        res = ws.run(command, "data")

    assert res.rc == 1, (
        f"{command}/{kind}: expected rc=1, got {res.rc} (out={res.out!r} err={res.err!r})"
    )
    assert expected in res.err.lower(), f"{command}/{kind}: stderr={res.err!r}"
    assert "traceback" not in res.err.lower(), f"{command}/{kind}: leaked a traceback:\n{res.err}"

    # Fail-safe means fail INERT: nothing on S3 or locally may have moved,
    # including the damaged manifest object itself - a command must not "fix"
    # it by publishing a fresh one over what it could not validate (push's
    # own _validate_before_publish guards the other direction: a freshly
    # written manifest is validated before upload, so a rejected download can
    # never be replaced with one that would only fail the same way next time).
    assert _object_snapshot(ws) == objects_before, f"{command}/{kind}: S3 state changed"
    assert _local_snapshot(ws.root / "data") == data_before, f"{command}/{kind}: local tree changed"
    if dest_before is not None:
        assert _local_snapshot(dest) == dest_before, (
            f"{command}/{kind}: pull mutated its destination"
        )


def test_show_never_reads_the_manifest_so_a_damaged_one_does_not_affect_it(pushed_ws):
    # `show` streams the data object directly and never calls
    # download_manifest, unlike every command in the matrix above - so it has
    # no place there. Pin the reason directly: even a manifest reduced to
    # garbage does not stop `show` from reading the (perfectly fine) object.
    ws = pushed_ws
    key = f"{ws.prefix}/data-manifest.jsonl"
    ws.s3.put_object(Bucket=ws.bucket, Key=key, Body=b"not a manifest at all")

    res = ws.run("show", "data/a.txt", expect_rc=0)
    assert res.out == "alpha"
