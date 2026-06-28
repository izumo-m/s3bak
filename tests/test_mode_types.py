"""Reading a manifest whose mode field carries S_IFMT (file-type) bits.

The write side still emits permission-only modes; these tests feed the new
format (full st_mode, e.g. ``100644``/``40755``) into the read/pull path
directly to confirm it interprets the type bits and stays compatible with the
old permission-only format.
"""

from __future__ import annotations

import datetime
import os
import stat

from s3bak.cli import parse_manifest_line, shell_always_quote

MTIME = 1_600_000_000


def _stamp(mtime: int) -> str:
    dt = datetime.datetime.fromtimestamp(mtime).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S") + ".000000000 " + dt.strftime("%z")


def _line(
    mode: str, name: str, *, mtime: int = MTIME, size: int = 0, sym: str | None = None
) -> str:
    field = shell_always_quote(name)
    if sym is not None:
        field += f" -> {shell_always_quote(sym)}"
    return f"{mode} owner group {size} {_stamp(mtime)} {field}"


def _put_manifest(ws, entry: str, lines: list[str]) -> None:
    body = ("\n".join(lines) + "\n").encode()
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/{entry}-ls-l.txt", Body=body)


# --- ManifestEntry mode accessors --------------------------------------------


def test_old_format_mode_has_no_type_bits():
    entry = parse_manifest_line(_line("644", "a.txt", size=5))
    assert entry is not None
    assert entry.has_type is False
    assert entry.is_dir is False
    assert entry.perm_bits == 0o644
    assert entry.perm_str == "644"


def test_new_format_regular_file_mode():
    entry = parse_manifest_line(_line("100644", "a.txt", size=5))
    assert entry is not None
    assert entry.has_type is True
    assert entry.is_dir is False
    assert entry.perm_bits == 0o644
    assert entry.perm_str == "644"


def test_new_format_directory_mode():
    entry = parse_manifest_line(_line("40755", "."))
    assert entry is not None
    assert entry.has_type is True
    assert entry.is_dir is True
    assert entry.perm_str == "755"


def test_perm_str_keeps_setuid_and_sticky_bits():
    # setuid + rwxr-xr-x on a regular file: type bits stripped, special bits kept.
    entry = parse_manifest_line(_line("104755", "s"))
    assert entry is not None
    assert entry.has_type is True
    assert entry.perm_bits == 0o4755
    assert entry.perm_str == "4755"


def test_malformed_mode_falls_back_to_legacy():
    entry = parse_manifest_line(_line("notoctal", "a.txt"))
    assert entry is not None
    assert entry.has_type is False  # never crashes; treated as old format
    assert entry.perm_str == "notoctal"


# --- status / pull against new-format manifests ------------------------------


def test_status_clean_against_new_format_manifest(ws):
    ws.write("data/a.txt", "hello")
    os.chmod(ws.root / "data", 0o755)
    os.chmod(ws.root / "data" / "a.txt", 0o640)
    os.utime(ws.root / "data" / "a.txt", (MTIME, MTIME))
    ws.config({"data": {"path": str(ws.root / "data")}})

    _put_manifest(
        ws,
        "data",
        [_line("40755", "."), _line("100640", "./a.txt", size=5)],
    )

    res = ws.run("status", "data", expect_rc=0)
    assert res.out.strip() == ""


def test_status_reports_type_mismatch_from_new_format(ws):
    # Manifest records a.txt as a directory; locally it is a regular file.
    ws.write("data/a.txt", "hello")
    os.chmod(ws.root / "data", 0o755)
    ws.config({"data": {"path": str(ws.root / "data")}})

    _put_manifest(
        ws,
        "data",
        [_line("40755", "."), _line("40755", "./a.txt")],
    )

    res = ws.run("status", "data", expect_rc=0)
    assert any(ln.startswith("D") and "a.txt" in ln for ln in res.out.splitlines())


def test_pull_new_format_applies_permission_bits(ws):
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


def test_pull_new_format_unuploaded_file_is_not_restored_as_dir(ws):
    # real.txt has a data object; ghost.txt is recorded but never uploaded.
    # The old object-presence heuristic would restore ghost.txt as a directory;
    # the recorded type bits keep it a (missing) regular file instead.
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/real.txt", Body=b"real")
    _put_manifest(
        ws,
        "data",
        [
            _line("40755", "."),
            _line("100644", "./real.txt", size=4),
            _line("100644", "./ghost.txt", size=9),
        ],
    )
    ws.config({"data": {"path": str(ws.root / "data")}})

    dest = ws.root / "out"
    res = ws.run("pull", "data", "-o", str(dest))

    assert (dest / "real.txt").read_text() == "real"
    assert not (dest / "ghost.txt").exists()  # never created as a directory
    assert res.rc != 0  # the un-uploaded file is reported missing


def test_pull_empty_dir_subpath_new_format_restores_directory(ws):
    # An empty directory recorded with type bits and no descendants: the sub-path
    # kind comes straight from the manifest, no head-object probe needed.
    _put_manifest(
        ws,
        "data",
        [_line("40755", "."), _line("40755", "./empty")],
    )
    ws.config({"data": {"path": str(ws.root / "data")}})

    dest = ws.root / "out"
    ws.run("pull", str(ws.root / "data" / "empty"), "-o", str(dest), expect_rc=0)
    assert dest.is_dir()
