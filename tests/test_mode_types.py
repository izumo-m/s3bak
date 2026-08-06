"""Recorded file types (full st_mode) drive status and restore decisions.

The v3 manifest always records the full st_mode; these tests feed hand-built
manifests into the read/pull path to pin how the type bits are interpreted
(dir vs file vs ghost), and check the permission-bit accessors.
"""

from __future__ import annotations

import json
import os
import stat

from s3bak import compare
from s3bak.manifest import parse_entry

MTIME_NS = 1_600_000_000 * 1_000_000_000


def _line(mode: str, rel: str, *, mtime_ns: int = MTIME_NS, size: int | None = None) -> str:
    obj: dict[str, object] = {"path": rel, "mode": mode, "owner": "owner", "group": "group"}
    if size is not None:
        obj["size"] = size
    obj["mtime_ns"] = mtime_ns
    return json.dumps(obj)


def _put_manifest(ws, entry: str, lines: list[str]) -> None:
    body = ("\n".join(['{"s3bak_manifest":3}', *lines]) + "\n").encode()
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/{entry}-manifest.jsonl", Body=body)


# --- ManifestEntry mode accessors --------------------------------------------


def test_regular_file_mode():
    entry = parse_entry(_line("100644", "./a.txt", size=5))
    assert entry is not None
    assert entry.is_file
    assert not entry.is_dir
    assert entry.perm_bits == 0o644
    assert entry.perm_str == "644"


def test_directory_mode():
    entry = parse_entry(_line("40755", "."))
    assert entry is not None
    assert entry.is_dir
    assert not entry.is_file
    assert entry.perm_str == "755"


def test_perm_str_keeps_setuid_and_sticky_bits():
    # setuid + rwxr-xr-x on a regular file: type bits stripped, special bits kept.
    entry = parse_entry(_line("104755", "./s"))
    assert entry is not None
    assert entry.is_file
    assert entry.perm_bits == 0o4755
    assert entry.perm_str == "4755"


# --- the shared mode predicate (status's mode report, push's manifest refresh) --


def _st(mode: int) -> os.stat_result:
    # Only st_mode matters to mode_differs.
    return os.stat_result((mode, 0, 0, 1, 0, 0, 0, 0, 0, 0))


def test_mode_differs_compares_permission_bits(monkeypatch):
    monkeypatch.setattr(compare, "IS_WINDOWS", False)
    entry = parse_entry(_line("100644", "./a.txt", size=5))
    assert entry is not None
    assert not compare.mode_differs(entry, _st(0o100644))
    assert compare.mode_differs(entry, _st(0o100600))


def test_mode_differs_windows_reads_only_owner_write_bit(monkeypatch):
    # Windows-native Python reports synthetic modes (0o666 writable, 0o444
    # read-only), so only the owner-write bit carries information there.
    monkeypatch.setattr(compare, "IS_WINDOWS", True)
    entry = parse_entry(_line("100644", "./a.txt", size=5))
    assert entry is not None
    assert not compare.mode_differs(entry, _st(0o100666))
    assert compare.mode_differs(entry, _st(0o100444))


# --- status / pull against hand-built manifests --------------------------------


def test_status_clean_against_manifest(ws):
    ws.write("data/a.txt", "hello")
    os.chmod(ws.root / "data", 0o755)
    os.chmod(ws.root / "data" / "a.txt", 0o640)
    os.utime(ws.root / "data" / "a.txt", ns=(MTIME_NS, MTIME_NS))
    os.utime(ws.root / "data", ns=(MTIME_NS, MTIME_NS))
    ws.config({"data": {"path": str(ws.root / "data")}})

    _put_manifest(
        ws,
        "data",
        [_line("40755", "."), _line("100640", "./a.txt", size=5)],
    )

    res = ws.run("status", "data", expect_rc=0)
    assert res.out.strip() == ""


def test_status_reports_type_mismatch(ws):
    # Manifest records a.txt as a directory; locally it is a regular file.
    # The two sort to different keys, so nothing pairs them: plain status
    # shows the local file as A (a push would add it), and the old record's
    # D surfaces under --delete alone.
    ws.write("data/a.txt", "hello")
    os.chmod(ws.root / "data", 0o755)
    ws.config({"data": {"path": str(ws.root / "data")}})

    _put_manifest(
        ws,
        "data",
        [_line("40755", "."), _line("40755", "./a.txt")],
    )

    res = ws.run("status", "data", expect_rc=0)
    lines = res.out.splitlines()
    assert any(ln.startswith("A") and "a.txt" in ln for ln in lines)
    assert not any(ln.startswith("D") for ln in lines)

    res = ws.run("status", "--delete", "data", expect_rc=0)
    assert any(ln.startswith("D") and "a.txt" in ln for ln in res.out.splitlines())


def test_pull_applies_permission_bits(ws):
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt", Body=b"hello")
    _put_manifest(
        ws,
        "data",
        [_line("40755", "."), _line("100640", "./a.txt", size=5)],
    )
    ws.config({"data": {"path": str(ws.root / "data")}})

    dest = ws.root / "out"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)

    assert (dest / "a.txt").read_text() == "hello"
    assert stat.S_IMODE((dest / "a.txt").stat().st_mode) == 0o640
    assert stat.S_IMODE(dest.stat().st_mode) == 0o755


def test_pull_restores_recorded_mtime_ns(ws):
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt", Body=b"hello")
    _put_manifest(
        ws,
        "data",
        [_line("40755", "."), _line("100644", "./a.txt", size=5)],
    )
    ws.config({"data": {"path": str(ws.root / "data")}})

    dest = ws.root / "out"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert os.lstat(dest / "a.txt").st_mtime_ns == MTIME_NS


def test_pull_unuploaded_file_is_not_restored_as_dir(ws):
    # real.txt has a data object; ghost.txt is recorded but never uploaded.
    # The recorded type keeps it a (missing) regular file - warned about and
    # skipped as a stale record, never silently created as a directory.
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/real.txt", Body=b"real")
    _put_manifest(
        ws,
        "data",
        [
            _line("40755", "."),
            _line("100644", "./ghost.txt", size=9),
            _line("100644", "./real.txt", size=4),
        ],
    )
    ws.config({"data": {"path": str(ws.root / "data")}})

    dest = ws.root / "out"
    res = ws.run("pull", "data", "-o", str(dest))

    assert (dest / "real.txt").read_text() == "real"
    assert not (dest / "ghost.txt").exists()  # never created as a directory
    assert "a push retires the stale record" in res.err  # warned, not fatal
    assert res.rc == 0  # run() maps the warning to exit 2


def test_pull_empty_dir_subpath_restores_directory(ws):
    # An empty directory has no descendants and no S3 object; its recorded
    # type alone decides the sub-path kind - no head-object probe.
    _put_manifest(
        ws,
        "data",
        [_line("40755", "."), _line("40755", "./empty")],
    )
    ws.config({"data": {"path": str(ws.root / "data")}})

    dest = ws.root / "out"
    ws.run("pull", str(ws.root / "data" / "empty"), "-o", str(dest), expect_rc=0)
    assert dest.is_dir()


def test_pull_replaces_file_where_directory_recorded(ws):
    # The manifest records ./conflict as a directory, but the restore target
    # already holds a regular file at that name. Restore must replace it with a
    # directory, not chmod the stray file and report success.
    _put_manifest(
        ws,
        "data",
        [_line("40755", "."), _line("40755", "./conflict")],
    )
    ws.config({"data": {"path": str(ws.root / "data")}})

    dest = ws.root / "out"
    dest.mkdir()
    (dest / "conflict").write_text("i am a file")  # wrong type at the dir path

    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert (dest / "conflict").is_dir()
